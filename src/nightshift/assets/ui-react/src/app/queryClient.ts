import { QueryClient } from '@tanstack/react-query'

/**
 * Shared QueryClient. Conservative defaults: a short staleTime so manually
 * navigating between screens re-validates, no refetch-on-window-focus storm
 * (the manager has SSE; the worker polls explicitly), and one retry.
 */
export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        refetchOnWindowFocus: false,
        retry: 1,
      },
    },
  })
}
