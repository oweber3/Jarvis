// Deleting everything asks the owner to type a word, so one stray click or key press cannot do it.
export const CONFIRM_WORD = "delete"

export function confirmWordTyped(typed: string, word: string = CONFIRM_WORD): boolean {
  return typed.trim().toLowerCase() === word.toLowerCase()
}
