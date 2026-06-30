import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import { makeQueryClient } from '../src/app/queryClient'
import { ManagerApp } from './ManagerApp'
import '../src/theme.css'

const queryClient = makeQueryClient()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ManagerApp />
    </QueryClientProvider>
  </StrictMode>,
)
