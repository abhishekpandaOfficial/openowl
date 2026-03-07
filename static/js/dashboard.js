(() => {
  const wsState = document.getElementById('ws-state');
  const msgCount = document.getElementById('msg-count');
  const log = document.getElementById('log');
  if (!wsState || !msgCount || !log) return;

  let count = 0;
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${protocol}://${location.host}/ws/dashboard`);

  const addLog = (type, payload) => {
    const row = document.createElement('div');
    row.className = 'log-item';
    const time = new Date().toLocaleTimeString();
    row.innerHTML = `<span class="muted">${time}</span> <strong>[${type}]</strong> ${payload}`;
    log.prepend(row);
    while (log.children.length > 120) {
      log.removeChild(log.lastChild);
    }
  };

  ws.addEventListener('open', () => {
    wsState.textContent = 'LIVE';
    wsState.className = 'ok';
    addLog('SYSTEM', 'Dashboard websocket connected');
  });

  ws.addEventListener('close', () => {
    wsState.textContent = 'DISCONNECTED';
    wsState.className = 'warn';
    addLog('SYSTEM', 'Dashboard websocket disconnected');
  });

  ws.addEventListener('message', (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data.type === 'ping') return;
      count += 1;
      msgCount.textContent = String(count);
      const payload = JSON.stringify(data.data || data.message || '').slice(0, 160);
      addLog((data.type || 'event').toUpperCase(), payload);
    } catch {
      addLog('ERROR', 'Invalid websocket payload');
    }
  });
})();
