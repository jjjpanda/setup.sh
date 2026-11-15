"""CPU preview of right-justified little-endian RAW10 in BGGR halfwords.

Each 2x2 Bayer block becomes one RGB pixel; the two greens are averaged. This
is a simple preview conversion, without an ISP or calibrated black subtraction.
Optional bounded gray-world white balance adjusts display RGB only; it assumes
the scene averages to gray and is not a calibrated sensor color matrix.
Optional global percentile stretching is diagnostic and
must not be presented as corrected sensor exposure. Only standard Python is
required; the returned PPM can be decoded by the existing image viewer.
"""
import array
import hashlib
import math
import sys


MAX_PIXELS = 8 * 1024 * 1024


def decode_le16(raw, width, height):
    """Validate exact unpacked RAW10 geometry and return host-order samples."""
    if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0
            or width % 2 or height % 2 or width * height > MAX_PIXELS
            or len(raw) != width * height * 2):
        raise ValueError('Even geometry and exact RAW10 halfword byte length required')
    samples = array.array('H')
    if samples.itemsize != 2:
        raise ValueError('16-bit unsigned array required')
    samples.frombytes(raw)
    if sys.byteorder != 'little':
        samples.byteswap()
    if max(samples) > 1023:
        raise ValueError('Sample exceeds right-justified RAW10 range')
    return samples


def _quantile(histogram, numerator, denominator=100):
    target = (sum(histogram) - 1) * numerator // denominator
    cumulative = 0
    for sample, count in enumerate(histogram):
        cumulative += count
        if cumulative > target:
            return sample
    raise ValueError('Empty sample histogram')


def gray_world_gains(samples, width, height, black_level):
    """Estimate display gains from at most 4096 unclipped Bayer blocks.

    Green is the reference. Dark/clipped blocks do not inform the estimate;
    a sparse/dark frame falls back to unity. Bounds prevent extreme corrections
    in strongly colored scenes, but cannot establish that a scene is neutral.
    """
    step = max(1, math.ceil(math.sqrt((width // 2) * (height // 2) / 4096)))
    while ((width // 2 + step - 1) // step) * ((height // 2 + step - 1) // step) > 4096:
        step += 1
    red = green = blue = count = 0
    for y in range(0, height, 2 * step):
        upper, lower = y * width, (y + 1) * width
        for x in range(0, width, 2 * step):
            b, ga, gb, r = (samples[upper + x], samples[upper + x + 1],
                             samples[lower + x], samples[lower + x + 1])
            if min(b, ga, gb, r) <= black_level + 8 or max(b, ga, gb, r) >= 1000:
                continue
            red += r - black_level
            green += (ga + gb) / 2 - black_level
            blue += b - black_level
            count += 1
    gains = (max(0.25, min(4., green / red)), 1.,
             max(0.25, min(4., green / blue))) if count >= 16 else (1., 1., 1.)
    return gains, count


def bggr_superpixels(samples, width, height, *, low=0, high=1023, srgb=True,
                     gains=(1., 1., 1.)):
    """Return packed RGB bytes at half the input width and height."""
    if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0
            or width % 2 or height % 2 or width * height > MAX_PIXELS
            or len(samples) != width * height or not 0 <= low < high <= 1023
            or len(gains) != 3 or any(not math.isfinite(g) or not 0.25 <= g <= 4 for g in gains)):
        raise ValueError('Invalid Bayer geometry or display range')
    if min(samples) < 0 or max(samples) > 1023:
        raise ValueError('Sample exceeds RAW10 range')
    def display(value, gain):
        value = max(0., min(1., (value - low) * gain / (high - low)))
        if srgb:
            value = 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
        return round(value * 255)
    lookups = [bytes(display(value, gain) for value in range(1024)) for gain in gains]
    red_lookup, green_lookup, blue_lookup = lookups
    output = bytearray(width * height * 3 // 4)
    position = 0
    row_bytes = width * 3 // 2
    for y in range(0, height, 2):
        upper = y * width
        lower = upper + width
        end = position + row_bytes
        # Bulk channel assignment avoids half a million tiny bytes objects,
        # slice writes and repeated per-pixel min/max checks on the ARM CPU.
        output[position:end:3] = bytes(red_lookup[v] for v in samples[lower + 1:lower + width:2])
        output[position + 1:end:3] = bytes(green_lookup[(a + b + 1) // 2] for a, b in zip(
            samples[upper + 1:upper + width:2], samples[lower:lower + width:2]))
        output[position + 2:end:3] = bytes(blue_lookup[v] for v in samples[upper:upper + width:2])
        position = end
    return bytes(output)


def raw10_bggr_to_ppm(raw, width, height, *, auto_levels=False, srgb=True, black_level=0,
                      white_balance=False):
    """Return (PPM, metadata), preserving the supplied raw bytes unchanged.

    Defaults display the full 0..1023 sample range. auto_levels=True uses one
    shared 1st..99th percentile display range, without channel balancing; that
    metadata explicitly marks diagnostic stretching. Flat frames retain the
    full range rather than manufacturing contrast. black_level sets the fixed
    display floor (sensor pedestal); it is not a stretch or white balance.
    """
    if type(black_level) is not int or not 0 <= black_level < 1023:
        raise ValueError('Black level must be an integer in 0..1022')
    samples = decode_le16(raw, width, height)
    histogram = [0] * 1024
    for sample in samples:
        histogram[sample] += 1
    p01, p99 = _quantile(histogram, 1), _quantile(histogram, 99)
    applied = bool(auto_levels and p01 < p99)
    low, high = (p01, p99) if applied else (black_level, 1023)
    gains, count = gray_world_gains(samples, width, height, black_level) if white_balance else ((1., 1., 1.), 0)
    pixels = bggr_superpixels(samples, width, height, low=low, high=high, srgb=srgb, gains=gains)
    output_width, output_height = width // 2, height // 2
    ppm = f'P6\n{output_width} {output_height}\n255\n'.encode('ascii') + pixels
    minimum = next(i for i, count in enumerate(histogram) if count)
    maximum = next(i for i in range(1023, -1, -1) if histogram[i])
    report = {
        'input': {'width': width, 'height': height, 'bits': 10,
                  'byte_order': 'little', 'packing': 'right-justified 16-bit halfword', 'bayer': 'BGGR'},
        'output': {'width': output_width, 'height': output_height, 'format': 'PPM'},
        'method': '2x2 Bayer blocks; red, average green pair, blue',
        'black_level_calibration': False, 'fixed_black_level': black_level,
        'white_balance': bool(white_balance and count >= 16),
        'white_balance_details': {'requested': bool(white_balance),
                                  'method': 'bounded gray-world' if white_balance else 'none',
                                  'gains_rgb': list(gains), 'eligible_blocks': count,
                                  'calibrated_color_matrix': False},
        'diagnostic_auto_levels': applied, 'display_range': [low, high], 'srgb_transfer': bool(srgb),
        'samples': {'min': minimum, 'max': maximum,
                    'mean': sum(i * n for i, n in enumerate(histogram)) / len(samples),
                    'p01': p01, 'p99': p99},
        'raw_sha256': hashlib.sha256(raw).hexdigest(),
        'ppm_sha256': hashlib.sha256(ppm).hexdigest(),
    }
    return ppm, report
