import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ExternalLink, Sparkles } from 'lucide-react'
import TryOnView from './TryOnView'

function formatPrice(price, currency) {
  if (price == null) return null
  const symbol = currency === 'USD' ? '$' : currency === 'EUR' ? '€' : currency || '₹'
  return `${symbol}${price.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
}

export default function ProductGrid({ products, sessionId }) {
  const [tryOnProduct, setTryOnProduct] = useState(null)

  if (!products || products.length === 0) {
    return (
      <p className="text-sm text-muted italic py-2">
        No matching pieces found — try widening the criteria.
      </p>
    )
  }

  return (
    <>
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 mt-2">
        {products.map((p, i) => (
          <motion.div
            key={p.id || i}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, delay: i * 0.04 }}
            className="group bg-surfaceRaised border border-hairline rounded-xl overflow-hidden hover:border-brass transition-colors"
          >
            <a href={p.product_url} target="_blank" rel="noopener noreferrer">
              <div className="aspect-square bg-ink/40 overflow-hidden">
                {p.image_url ? (
                  <img
                    src={p.image_url}
                    alt={p.title}
                    loading="lazy"
                    className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"
                  />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-muted text-xs">
                    No image
                  </div>
                )}
              </div>
            </a>
            <div className="p-2.5">
              <p className="text-xs text-ivory leading-snug line-clamp-2 mb-1.5">{p.title}</p>
              <div className="flex items-center justify-between mb-2">
                <span className="font-mono text-sm text-brass">
                  {formatPrice(p.price, p.currency) || '—'}
                </span>
                <a href={p.product_url} target="_blank" rel="noopener noreferrer">
                  <ExternalLink size={12} className="text-muted hover:text-brass transition-colors" />
                </a>
              </div>
              {/* Try On is only meaningful once a session/backend is available to
                  prep the cutout — sessionId is required. */}
              {sessionId && p.image_url && (
                <button
                  onClick={() => setTryOnProduct(p)}
                  className="w-full flex items-center justify-center gap-1.5 text-[11px] font-medium text-brass border border-brass/40 rounded-lg py-1.5 hover:bg-brass hover:text-ink transition-colors"
                >
                  <Sparkles size={12} />
                  Try it on
                </button>
              )}
            </div>
          </motion.div>
        ))}
      </div>

      <AnimatePresence>
        {tryOnProduct && (
          <TryOnView
            product={tryOnProduct}
            sessionId={sessionId}
            onClose={() => setTryOnProduct(null)}
          />
        )}
      </AnimatePresence>
    </>
  )
}
