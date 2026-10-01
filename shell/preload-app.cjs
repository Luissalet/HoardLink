'use strict';
// Lets an app know it runs inside a Hoard window (no Node access).
const { contextBridge } = require('electron');
contextBridge.exposeInMainWorld('hoardWindow', { shell: 'hoard-window', version: '0.1.0', platform: process.platform });
window.addEventListener('DOMContentLoaded', () => {
  document.documentElement.dataset.hoardWindow = '1';
});
