"""Persist the input selected in Studio and keep the running route in sync."""
import asyncio
import json
import os
from pathlib import Path
import tempfile


SOURCES = ('onboard', 'external', 'both')


class MicrophoneSettings:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / 'microphone-source.json' if state_dir else None
        self.lock = asyncio.Lock()

    def _saved(self):
        try:
            with self.path.open('rb') as stream:
                raw = stream.read(257)
            if len(raw) > 256:
                return None
            data = json.loads(raw)
        except (FileNotFoundError, ValueError, UnicodeError, RecursionError):
            return None
        if isinstance(data, dict) and set(data) == {'source'} and data['source'] in SOURCES:
            return data['source']
        return None

    def _save(self, source):
        with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent, prefix='.microphone-',
                                         delete=False, encoding='utf-8') as tmp:
            scratch = Path(tmp.name)
            try:
                json.dump({'source': source}, tmp)
                tmp.write('\n')
                tmp.flush()
                os.fsync(tmp.fileno())
            except BaseException:
                tmp.close()
                scratch.unlink(missing_ok=True)
                raise
        try:
            os.replace(scratch, self.path)
        finally:
            scratch.unlink(missing_ok=True)

    async def apply(self, source, request):
        if source not in SOURCES:
            raise ValueError('Please select onboard, external, or dual microphones')
        if self.path is None:
            raise RuntimeError('Microphone settings directory is not configured')
        # A closing browser must not interrupt a route change between the two writes.
        job = asyncio.create_task(self._apply(source, request))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            await job
            raise

    async def _apply(self, source, request):
        async with self.lock:
            status = await request({'method': 'status'})
            old = status.get('microphone_source')
            if 'microphone_source' not in status or (old is not None and old not in SOURCES):
                raise RuntimeError('Unable to confirm current microphone input; check audio device status and retry')
            saved = self._saved()
            recovery = old or saved or 'onboard'
            try:
                if source != old:
                    status = await request({'method': 'microphone', 'source': source})
                    if status.get('microphone_source') != source:
                        raise RuntimeError('Audio service did not confirm selected input')
                if source != saved:
                    try:
                        self._save(source)
                    except OSError as error:
                        raise RuntimeError('Failed to save microphone settings') from error
            except Exception:
                if old != source:
                    try:
                        restored = await request({'method': 'microphone', 'source': recovery})
                        if restored.get('microphone_source') != recovery:
                            raise RuntimeError('Audio service did not confirm recovery input')
                    except Exception as restore_error:
                        raise RuntimeError('Switch failed and original input recovery could not be confirmed; refresh and check audio device') from restore_error
                raise
            return status
