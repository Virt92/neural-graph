// Preload — bridge between main and renderer via contextBridge
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  onGraph: (cb) => ipcRenderer.on('graph:data', (_e, data) => cb(data)),
  onSessions: (cb) => ipcRenderer.on('sessions:data', (_e, data) => cb(data)),
  onTranscripts: (cb) => ipcRenderer.on('transcripts:data', (_e, data) => cb(data)),
  onInfra: (cb) => ipcRenderer.on('infra:data', (_e, data) => cb(data)),
  onHealth: (cb) => ipcRenderer.on('health:data', (_e, data) => cb(data)),
  onGrowth: (cb) => ipcRenderer.on('growth:data', (_e, data) => cb(data)),
  onSignals: (cb) => ipcRenderer.on('signals:data', (_e, data) => cb(data)),
  onAnomalies: (cb) => ipcRenderer.on('anomalies:data', (_e, data) => cb(data)),
  onProcesses: (cb) => ipcRenderer.on('processes:data', (_e, data) => cb(data)),
  onTraffic: (cb) => ipcRenderer.on('traffic:events', (_e, data) => cb(data)),
  requestInitial: () => ipcRenderer.invoke('request:initial'),
  isElectron: true,
});
