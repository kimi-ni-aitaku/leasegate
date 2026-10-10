import {defineConfig} from '@playwright/test';
export default defineConfig({testDir:'tests/browser',workers:1,timeout:30000,
  use:{baseURL:process.env.BASE_URL||'http://127.0.0.1:8080',viewport:{width:1365,height:1000},
    launchOptions:{...(process.env.CHROME_EXECUTABLE?{executablePath:process.env.CHROME_EXECUTABLE}:{}),
      args:process.env.CAMERA_QR_FILE?['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream',`--use-file-for-fake-video-capture=${process.env.CAMERA_QR_FILE}`]:[]}},
  reporter:[['list']]});
