import type { Confirmation, PhoneSnapshot } from "@/api"

/** The desktop confirmation this phone may answer now, or null (none waits, or answering from phones is off). */
export function pendingConfirmation(phone: PhoneSnapshot | null): Confirmation | null {
  return phone?.allow_confirm ? (phone.confirmation ?? null) : null
}
