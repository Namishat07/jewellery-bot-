/**
 * api.js — thin fetch wrappers around the FastAPI backend.
 * In dev, Vite proxies /api -> http://localhost:8000 (see vite.config.js).
 * In production, the frontend is served BY the same FastAPI app, so
 * relative /api paths work unchanged with no config needed.
 */

const BASE = '/api'

export async function createSession(url) {
  const res = await fetch(`${BASE}/session`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Something went wrong.' }))
    throw new Error(err.detail || `Request failed (${res.status})`)
  }
  return res.json()
}

/**
 * Streams a chat response. Calls onChunk(text) for each token as it
 * arrives, and onDone() once the stream finishes.
 */
export class SessionExpiredError extends Error {
  constructor() {
    super('This session expired (the server restarted). Re-scanning the site…')
    this.name = 'SessionExpiredError'
  }
}

export async function streamChat(sessionId, message, onChunk, onDone) {
  const res = await fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message }),
  })
  // The backend drops in-memory sessions on restart and after 1h idle. That is a
  // 404, not an unreachable backend — say so, so it can be recovered from.
  if (res.status === 404) throw new SessionExpiredError()
  if (!res.ok || !res.body) {
    throw new Error(`Chat request failed (${res.status}).`)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    const parts = buffer.split('\n\n')
    buffer = parts.pop() // keep incomplete trailing chunk for next read

    for (const part of parts) {
      if (part.startsWith('event: done')) {
        onDone?.()
      } else if (part.startsWith('event: error')) {
        const detail = part.split('data: ')[1] || 'The assistant backend failed.'
        throw new Error(detail)
      } else if (part.startsWith('data: ')) {
        onChunk(part.slice(6))
      }
    }
  }
  onDone?.()
}

export async function filterProducts(sessionId, filters) {
  const res = await fetch(`${BASE}/filter`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, ...filters }),
  })
  if (!res.ok) throw new Error('Filter request failed.')
  return res.json()
}

export async function imageSearch(sessionId, file) {
  const formData = new FormData()
  formData.append('session_id', sessionId)
  formData.append('image', file)
  const res = await fetch(`${BASE}/image-search`, { method: 'POST', body: formData })
  if (res.status === 404) throw new SessionExpiredError()
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || `Image search failed (${res.status}).`)
  }
  return res.json()
}
