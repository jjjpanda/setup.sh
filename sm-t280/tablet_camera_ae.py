"""Hardware-free metering and bounded SR259 still-exposure decisions.

Uses only the observed 333/1296-line exposures and verified 1/2/4/8 gains.
No sensor access, frame-length extension, pixel stretching or WB changes.
"""
import math
import struct


EXPOSURES = (333, 1296)
GAINS = (1, 2, 4, 8)
# Short exposure at4x/8x nearly duplicates long exposure at1x/2x. Prefer the
# lower analog gain instead of spending extra noise for <3% integration change.
SETTINGS = ((333, 1), (333, 2), *((1296, gain) for gain in GAINS))
MAX_ATTEMPTS = 3
BLACK_LEVEL = 64
TARGET = 0.18
MIN_SIGNAL, MAX_SIGNAL = 0.13, 0.26
# Small bright sources (lamps, windows) clip in mixed light. Reduce only when a
# large area is clipped, so a lamp does not darken the whole foreground.
MAX_CLIPPED = 0.10


def validate_setting(exposure_lines, gain):
    if (type(exposure_lines) is not int or exposure_lines not in EXPOSURES
            or type(gain) is not int or gain not in GAINS):
        raise ValueError('Only verified exposure333/1296 and gain1/2/4/8 are allowed')


def meter(raw, width, height):
    """Read at most4096 BGGR blocks, before display transfer and white balance.

    Green p60 estimates scene brightness, while all four channels inform the
    clipped-block fraction. This is scene metering, not calibrated luminance.
    Samples below the fixed pedestal remain zero rather than being stretched.
    """
    if (type(width) is not int or type(height) is not int or width < 2 or height < 2
            or width % 2 or height % 2 or width * height > 16_777_216
            or len(raw) != width * height * 2):
        raise ValueError('Exact even RAW10 halfword geometry is required')
    step = max(1, math.ceil(math.sqrt((width // 2) * (height // 2) / 4096)))
    while ((width // 2 + step - 1) // step) * ((height // 2 + step - 1) // step) > 4096:
        step += 1
    green = []
    clipped = 0
    for y in range(0, height, 2 * step):
        for x in range(0, width, 2 * step):
            b, ga = struct.unpack_from('<HH', raw, 2 * (y * width + x))
            gb, r = struct.unpack_from('<HH', raw, 2 * ((y + 1) * width + x))
            if max(b, ga, gb, r) > 1023:
                raise ValueError('Metered sample exceeds RAW10 range')
            green.append(max(0., (ga + gb) / 2 - BLACK_LEVEL) / (1023 - BLACK_LEVEL))
            clipped += max(b, ga, gb, r) >= 1000
    green.sort()
    return {'method': 'sparse BGGR green p60; all-channel clipping guard',
            'sampled_blocks': len(green), 'green_p60': green[(len(green) - 1) * 60 // 100],
            'green_p95': green[(len(green) - 1) * 95 // 100],
            'clipped_fraction': clipped / len(green), 'fixed_black_level': BLACK_LEVEL,
            'calibrated_luminance': False}


def decide(measurement, exposure_lines, gain, visited=()):
    """Return a stop or a verified next tuple; never perform hardware I/O.

    Integration*gain is an approximate linear model. At most a4x change per
    decision avoids extreme jumps from noisy/dark/clipped metering. The caller
    enforces MAX_ATTEMPTS and completes all cleanup before applying a decision.
    """
    validate_setting(exposure_lines, gain)
    signal = measurement['green_p60']
    clipped = measurement['clipped_fraction']
    if (not math.isfinite(signal) or not 0 <= signal <= 1
            or not math.isfinite(clipped) or not 0 <= clipped <= 1):
        raise ValueError('Invalid bounded meter values')
    if MIN_SIGNAL <= signal <= MAX_SIGNAL and clipped <= MAX_CLIPPED:
        return {'stop': True, 'reason': 'within_target', 'converged': True}
    ratio = max(0.25, min(4., TARGET / max(signal, 1 / 959)))
    if clipped > MAX_CLIPPED:
        # Large clipped areas hide the true brightness. Reduce by at least one
        # stop; never amplify because a dark foreground masks a bright background.
        ratio = min(ratio, 0.5)
    current = exposure_lines * gain
    requested = current * ratio
    candidate = min(SETTINGS,
                    key=lambda item: (abs(math.log2(item[0] * item[1] / requested)), item[1]))
    if candidate == (exposure_lines, gain):
        return {'stop': True, 'reason': 'control_limit_or_quantization', 'converged': False}
    if candidate in visited:
        return {'stop': True, 'reason': 'would_repeat_setting', 'converged': False}
    return {'stop': False, 'reason': 'reduce_clipping' if clipped > MAX_CLIPPED else 'adjust_brightness',
            'converged': False, 'exposure_lines': candidate[0], 'gain': candidate[1],
            'requested_ratio': ratio}


def score(measurement):
    """Lower is better: log distance from target plus a large-clipping penalty."""
    signal = max(measurement['green_p60'], 1 / 959)
    return abs(math.log2(signal / TARGET)) + 10 * max(0., measurement['clipped_fraction'] - MAX_CLIPPED)


def run(capture, initial_gain=8):
    """Acquire <=3 independently cleaned frames through a caller-owned callback.

    capture(exposure_lines,gain,attempt) must return raw,width,height only after
    successful DMA AND sensor cleanup. Any failure propagates without retry.
    The caller holds exclusive ownership across this complete sequence.
    """
    exposure_lines, gain = 1296, initial_gain
    validate_setting(exposure_lines, gain)
    trials, visited, best = [], [], None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        frame = capture(exposure_lines, gain, attempt)
        measurement = meter(frame['raw'], frame['width'], frame['height'])
        visited.append((exposure_lines, gain))
        decision = decide(measurement, exposure_lines, gain, visited)
        trials.append({'attempt': attempt, 'exposure_lines': exposure_lines,
                       'gain': gain, 'meter': measurement, 'decision': decision,
                       'score': score(measurement)})
        if best is None or trials[-1]['score'] < best[1]['score']:
            best = frame, trials[-1]
        if decision['stop'] or attempt == MAX_ATTEMPTS:
            # Publish the best attempt, not the last one.
            return best[0], {'mode': 'bounded-front-still', 'attempts': trials,
                             'maximum_attempts': MAX_ATTEMPTS,
                             'converged': decision['converged'],
                             'stop_reason': decision['reason'] if decision['stop'] else 'attempt_limit',
                             'selected_attempt': best[1]['attempt'],
                             'final_exposure_lines': best[1]['exposure_lines'],
                             'final_gain': best[1]['gain'],
                             'white_balance_changed': False}
        exposure_lines, gain = decision['exposure_lines'], decision['gain']
