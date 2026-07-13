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
export async function streamChat(sessionId, message, onChunk, onDone) {
  const res = await fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message }),
  })
  if (!res.ok || !res.body) {
    throw new Error('Chat request failed.')
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
  if (!res.ok) throw new Error('Image search failed.')
  return res.json()
}
