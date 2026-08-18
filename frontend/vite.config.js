import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // 실행 환경이 PORT를 지정하면 그 포트로 바인딩(멀티 인스턴스 프리뷰 대응)
  server: process.env.PORT ? { port: Number(process.env.PORT) } : undefined,
})
