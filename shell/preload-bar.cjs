'use strict';
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('hoardBar', {
  menu: (x, y) => ipcRenderer.send('hoard:menu', { x, y }),
  home: () => ipcRenderer.send('hoard:home'),
  reload: () => ipcRenderer.send('hoard:reload'),
  on: (channel, fn) => {
    if (!['hoard:init', 'hoard:colours', 'hoard:title', 'hoard:status'].includes(channel)) return;
    ipcRenderer.on(channel, (_e, data) => fn(data));
  },
});
