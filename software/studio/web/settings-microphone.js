import { html, useEffect, useRef, useState } from './vendor/preact-htm.module.js';

const SOURCES = { onboard: 'Onboard Mic', external: 'External Mic (J2 Left)', both: 'Onboard + External' };

export function MicrophoneSettings({ api }) {
  const [data, setData] = useState(null);
  const [readError, setReadError] = useState('');
  const [writeError, setWriteError] = useState('');
  const [saving, setSaving] = useState(false);
  const select = useRef(null);

  useEffect(() => {
    let timer, flight, writing = false;
    async function refresh(source) {
      if (document.hidden || writing) return;
      clearTimeout(timer);
      flight?.abort();
      const ctl = new AbortController();
      flight = ctl;
      writing = source !== undefined;
      if (writing) {
        setSaving(true);
        setWriteError('');
      }
      let delay = writing ? 250 : 4000;
      try {
        const result = await api('/api/microphone', writing ? { source } : undefined,
          writing ? 15000 : 2500, ctl.signal);
        if (ctl.signal.aborted) return;
        if (!result.ok || !result.data) throw Error(result.error || 'Failed to read microphone settings.');
        setData(result.data);
        setReadError('');
        delay = result.data.listening ? 750 : 2000;
      } catch (error) {
        if (ctl.signal.aborted) return;
        setData(null);
        if (writing) setWriteError(error.message || 'Failed to switch microphone.');
        else setReadError(error.message || 'Failed to read microphone settings.');
      } finally {
        if (flight === ctl) {
          flight = null;
          writing = false;
          setSaving(false);
          if (!document.hidden) timer = setTimeout(refresh, delay);
        }
      }
    }
    function visibilityChanged() {
      clearTimeout(timer);
      if (writing) return;
      flight?.abort();
      flight = null;
      setData(null);
      if (!document.hidden) refresh();
    }
    select.current = refresh;
    document.addEventListener('visibilitychange', visibilityChanged);
    refresh();
    return () => {
      select.current = null;
      clearTimeout(timer);
      flight?.abort();
      flight = null;
      document.removeEventListener('visibilitychange', visibilityChanged);
    };
  }, [api]);

  const level = data?.microphone_level_dbfs;
  return html`<section class="board microphone-settings" aria-labelledby="microphone-title">
    <h2 id="microphone-title">Microphone Input</h2>
    <div class="microphone-sources" role="group" aria-label="Select microphone source">
      ${Object.entries(SOURCES).map(([source, label]) => html`<button type="button" key=${source}
        aria-pressed=${data?.microphone_source === source ? 'true' : 'false'} disabled=${saving || !data}
        onClick=${() => select.current?.(source)}>${label}</button>`)}
    </div>
    <p role="status">${saving ? 'Switching source...' : data?.microphone_source
      ? 'Active · ' + SOURCES[data.microphone_source] : data || readError || writeError ? 'Source unconfirmed' : 'Reading source...'}</p>
    ${data ? html`<p class="num">${!data.listening ? 'Listening disabled' : Number.isFinite(level)
      ? `Input level ${level.toFixed(1)} dBFS` : 'Listening; waiting for level...'}</p>` : null}
    ${(writeError || readError || data?.error) && html`<p role="alert">${[...new Set([writeError, readError, data?.error].filter(Boolean))].join(' ')}</p>`}
    <details><summary>Range comparison tips</summary>
      <p>Enable listening in Overview -> Voice & Music. Keep external mic outside the shell away from fan exhaust and test at 0.5m, 1m, and 2m.</p>
    </details>
  </section>`;
}
