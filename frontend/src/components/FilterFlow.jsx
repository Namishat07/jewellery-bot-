import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Gem, Check } from 'lucide-react'
import { filterProducts } from '../lib/api'
import ProductGrid from './ProductGrid'

const STEPS = [
  { key: 'occasion', label: 'Occasion', options: ['Wedding', 'Engagement', 'Party', 'Daily wear', 'Festive', 'Gift', 'Any'] },
  { key: 'jewellery_type', label: 'Type', options: ['Ring', 'Necklace', 'Earrings', 'Bracelet', 'Bangle', 'Pendant', 'Any'] },
  { key: 'material', label: 'Material', options: ['Gold', 'Silver', 'Platinum', 'Diamond', 'Pearl', 'Gemstone', 'Any'] },
  { key: 'budget', label: 'Budget', options: ['Under ₹5,000', '₹5,000–20,000', '₹20,000–50,000', 'Above ₹50,000', 'Any'] },
]

function budgetToRange(label) {
  switch (label) {
    case 'Under ₹5,000': return { min_price: 0, max_price: 5000 }
    case '₹5,000–20,000': return { min_price: 5000, max_price: 20000 }
    case '₹20,000–50,000': return { min_price: 20000, max_price: 50000 }
    case 'Above ₹50,000': return { min_price: 50000, max_price: null }
    default: return {}
  }
}

export default function FilterFlow({ sessionId, onComplete }) {
  const [stepIndex, setStepIndex] = useState(0)
  const [answers, setAnswers] = useState({})
  const [loading, setLoading] = useState(false)
  const [results, setResults] = useState(null)

  const step = STEPS[stepIndex]
  const isLastStep = stepIndex === STEPS.length - 1

  async function selectOption(option) {
    const updated = { ...answers, [step.key]: option }
    setAnswers(updated)

    if (!isLastStep) {
      setStepIndex(stepIndex + 1)
      return
    }

    setLoading(true)
    const filters = {
      occasion: updated.occasion !== 'Any' ? updated.occasion?.toLowerCase() : null,
      jewellery_type: updated.jewellery_type !== 'Any' ? updated.jewellery_type?.toLowerCase() : null,
      material: updated.material !== 'Any' ? updated.material?.toLowerCase() : null,
      ...budgetToRange(updated.budget),
    }
    try {
      const data = await filterProducts(sessionId, filters)
      setResults(data.matches)
      onComplete?.(data.matches)
    } catch {
      setResults([])
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex gap-3">
      <div className="hallmark mt-0.5">
        <Gem size={13} className="text-brass" />
      </div>

      <div className="max-w-[85%] sm:max-w-[75%] w-full bg-surface border border-hairline rounded-2xl rounded-tl-sm px-4 py-3.5">
        {/* step progress */}
        <div className="flex items-center gap-1.5 mb-3">
          {STEPS.map((s, i) => (
            <div
              key={s.key}
              className={`h-1 flex-1 rounded-full transition-colors ${
                i < stepIndex || results ? 'bg-brass' : i === stepIndex ? 'bg-brass/50' : 'bg-hairline'
              }`}
            />
          ))}
        </div>

        {!results && (
          <AnimatePresence mode="wait">
            <motion.div
              key={step.key}
              initial={{ opacity: 0, x: 10 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -10 }}
              transition={{ duration: 0.2 }}
            >
              <p className="font-mono text-[10px] tracking-[0.15em] uppercase text-muted mb-2">
                Step {stepIndex + 1} of {STEPS.length} — {step.label}
              </p>
              <div className="flex flex-wrap gap-2">
                {step.options.map((opt) => (
                  <button
                    key={opt}
                    onClick={() => selectOption(opt)}
                    disabled={loading}
                    className="px-3 py-1.5 text-xs rounded-full border border-hairline text-ivory hover:border-brass hover:bg-brass/10 transition-colors disabled:opacity-40"
                  >
                    {opt}
                  </button>
                ))}
              </div>
            </motion.div>
          </AnimatePresence>
        )}

        {loading && (
          <p className="text-xs text-muted font-mono mt-2">Finding matches…</p>
        )}

        {results && (
          <div>
            <p className="text-xs text-muted flex items-center gap-1.5 mb-1">
              <Check size={12} className="text-brass" /> Matched on your preferences
            </p>
            <ProductGrid products={results} />
          </div>
        )}
      </div>
    </div>
  )
}
