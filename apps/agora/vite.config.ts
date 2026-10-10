import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({plugins:[react()],base:'./',server:{port:62840,strictPort:true,proxy:{'/api':{target:process.env.AGORA_HUB_URL || 'http://127.0.0.1:8810',changeOrigin:true,configure(proxy){proxy.on('proxyReq',req=>{req.setHeader('origin',process.env.AGORA_HUB_URL || 'http://127.0.0.1:8810');req.setHeader('sec-fetch-site','same-origin');});}}}}});
