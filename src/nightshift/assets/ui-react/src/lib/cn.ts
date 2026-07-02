/** Join class names, dropping falsy values. The whole of our classnames need. */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}
