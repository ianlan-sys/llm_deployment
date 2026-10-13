type SessionInvalidationListener = () => void

const listeners = new Set<SessionInvalidationListener>()

export function notifySessionInvalidated() {
  for (const listener of listeners) listener()
}

export function onSessionInvalidated(listener: SessionInvalidationListener) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
