import { useState } from 'react'
import { motion } from 'framer-motion'
import { Gem, ArrowRight, Loader2 } from 'lucide-react'
import { createSession } from '../lib/api'

export default function LandingScreen({ onSessionCreated }) {
  const [url, setUrl] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [progressLabel, setProgressLabel] = useState('')

  async function handleSubmit(e) {
    e.preventDefault()
    if (!url.trim() || loading) return
    setError(null)
    setLoading(true)
    setProgressLabel('Reading the storefront…')

    const progressTimer = setTimeout(
      () => setProgressLabel('Cataloguing pieces — larger stores take a little longer…'),
      6000
    )

    try {
      const data = await createSession(url.trim())
      onSessionCreated(data)
    } catch (err) {
      setError(err.message || 'Could not read that site. Check the URL and try again.')
    } finally {
      clearTimeout(progressTimer)
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex flex-col items-center justify-center px-6 relative overflow-hidden">
      <div className="facet-glow" aria-hidden="true" />

      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, ease: 'easeOut' }}
        className="relative z-10 w-full max-w-xl text-center"
      >
        <div className="inline-flex items-center gap-2 mb-6 text-brass">
          <Gem size={20} strokeWidth={1.5} />
          <span className="font-mono text-xs tracking-[0.2em] uppercase text-muted">
            Personal Jewellery Assistant
          </span>
        </div>

        <h1 className="font-display text-4xl sm:text-5xl leading-[1.1] mb-4 text-ivory">
          Paste any jewellery site.
          <br />
          <span className="text-brass">Get its own concierge.</span>
        </h1>

        <p className="text-muted text-base mb-10 max-w-md mx-auto leading-relaxed">
          Enter a store's URL and I'll become your personal assistant for that
          site — its catalogue, its policies, its pieces. Nothing else.
        </p>

        <form onSubmit={handleSubmit} className="relative">
          <div className="relative flex items-center gap-2 bg-surface border border-hairline rounded-full px-2 py-2 focus-within:border-brass transition-colors">
            <input
              type="text"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="www.your-jewellery-store.com"
              disabled={loading}
              className="flex-1 bg-transparent px-4 py-2.5 text-ivory placeholder:text-muted/60 outline-none font-sans text-sm sm:text-base"
            />
            <button
              type="submit"
              disabled={loading || !url.trim()}
              className="flex items-center justify-center w-11 h-11 rounded-full bg-brass text-ink disabled:opacity-40 disabled:cursor-not-allowed hover:bg-brassBright transition-colors shrink-0"
              aria-label="Enter site"
            >
              {loading ? (
                <Loader2 size={18} className="animate-spin" />
              ) : (
                <ArrowRight size={18} />
              )}
            </button>
          </div>
        </form>

        {loading && (
          <p className="mt-4 text-sm text-muted font-mono animate-fadeUp" aria-live="polite">
            {progressLabel}
          </p>
        )}

        {error && (
          <p className="mt-4 text-sm text-garnet animate-fadeUp" role="alert">
            {error}
          </p>
        )}
      </motion.div>
    </div>
  )
}
