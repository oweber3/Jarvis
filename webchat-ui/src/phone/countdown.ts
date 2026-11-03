// The seconds a pending confirmation has left, counted down on the phone from what the PC said at the last poll.

export function deadlineFor(expiresIn: number, now: number): number {
  return now + Math.max(0, expiresIn) * 1000
}

export function secondsLeft(deadline: number, now: number): number {
  return Math.max(0, Math.ceil((deadline - now) / 1000))
}
