import { motion } from 'framer-motion'
import { ExternalLink } from 'lucide-react'

function formatPrice(price, currency) {
  if (price == null) return null
  const symbol = currency === 'USD' ? '$' : currency === 'EUR' ? '€' : currency || '₹'
  return `${symbol}${price.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
}

export default function ProductGrid({ products }) {
  if (!products || products.length === 0) {
    return (
      <p className="text-sm text-muted italic py-2">
        No matching pieces found — try widening the criteria.
      </p>
    )
  }

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 mt-2">
      {products.map((p, i) => (
        <motion.a
          key={p.id || i}
          href={p.product_url}
          target="_blank"
          rel="noopener noreferrer"
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.3, delay: i * 0.04 }}
          className="group bg-surfaceRaised border border-hairline rounded-xl overflow-hidden hover:border-brass transition-colors"
        >
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
          <div className="p-2.5">
            <p className="text-xs text-ivory leading-snug line-clamp-2 mb-1.5">{p.title}</p>
            <div className="flex items-center justify-between">
              <span className="font-mono text-sm text-brass">
                {formatPrice(p.price, p.currency) || '—'}
              </span>
              <ExternalLink size={12} className="text-muted group-hover:text-brass transition-colors" />
            </div>
          </div>
        </motion.a>
      ))}
    </div>
  )
}
