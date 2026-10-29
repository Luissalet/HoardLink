'use strict';
// Lets an app know it runs inside a Hoard window (no Node access).
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('hoardWindow', {
  shell: 'hoard-window',
  version: '0.2.0',
  platform: process.platform,
  // Drag a local file out of the app as a real file (call it from a dragstart
  // handler after preventDefault): any program accepts the drop.
  startDrag: (file) => ipcRenderer.send('hoard:start-drag', String(file || '')),
});
window.addEventListener('DOMContentLoaded', () => {
  document.documentElement.dataset.hoardWindow = '1';
});
