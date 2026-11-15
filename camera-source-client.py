"""Relay a running tablet Camera Stream application through SSH."""

import json
import os
from pathlib import Path
import select
import socket
import sys


def main():
    path = Path.home() / '.cache/camera-stream/source.sock'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as source:
        try:
            source.connect(str(path))
        except OSError as error:
            raise RuntimeError('Open Camera Stream from the tablet app picker first.') from error
        source.sendall(json.dumps({'camera': CAMERA}).encode() + b'\n')
        header = bytearray()
        while not header.endswith(b'\n') and len(header) < 1024:
            chunk = source.recv(1)
            if not chunk:
                raise RuntimeError('Camera Stream closed before accepting the viewer')
            header.extend(chunk)
        response = json.loads(header)
        if not response.get('ok'):
            raise RuntimeError(response.get('error', 'Camera Stream rejected the viewer'))
        session = response.get('session')
        if not session:
            raise RuntimeError('Restart the tablet Camera Stream app to enable switching')
        def control(action):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as status:
                status.settimeout(5)
                status.connect(str(path))
                status.sendall(json.dumps({action: session}).encode() + b'\n')
                with status.makefile('rb') as reply:
                    return json.loads(reply.readline(1024))
        def completed():
            result = control('status')
            if not result.get('ok') or result.get('result') != 0:
                raise RuntimeError('Camera cleanup failed; refusing another sensor')
        stopping = False
        while True:
            readable, _, _ = select.select([source] if stopping else [0, source], [], [], 1)
            if 0 in readable:
                # A control EOF requests cleanup; wait for the server to close
                # the data stream before reporting success to the switcher.
                if not control('stop').get('ok'):
                    raise RuntimeError('Camera Stream refused the cleanup request')
                stopping = True
            if source in readable:
                chunk = source.recv(65536)
                if not chunk:
                    completed()
                    return
                view = memoryview(chunk)
                while view:
                    count = os.write(1, view)
                    view = view[count:]


if __name__ == '__main__':
    main()
