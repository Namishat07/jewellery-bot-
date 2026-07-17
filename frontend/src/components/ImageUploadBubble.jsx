import { useState, useRef } from 'react'
import { motion } from 'framer-motion'
import { Gem, Upload, Loader2, X } from 'lucide-react'
import { imageSearch } from '../lib/api'
import ProductGrid from './ProductGrid'

export default function ImageUploadBubble({ sessionId }) {
  const [preview, setPreview] = useState(null)
  const [loading, setLoading] = useState(false)
  const [results, setResults] = useState(null)
  const [error, setError] = useState(null)
  const inputRef = useRef(null)

  async function handleFile(file) {
    if (!file || !file.type.startsWith('image/')) return
    setPreview(URL.createObjectURL(file))
    setError(null)
    setLoading(true)
    try {
      const data = await imageSearch(sessionId, file)
      setResults(data.matches)
    } catch {
      setError("Couldn't read that image — try another photo.")
    } finally {
      setLoading(false)
    }
  }

  function handleDrop(e) {
    e.preventDefault()
    handleFile(e.dataTransfer.files?.[0])
  }

  return (
    <div className="flex gap-3">
      <div className="hallmark mt-0.5">
        <Gem size={13} className="text-brass" />
      </div>

      <div className="max-w-[85%] sm:max-w-[75%] w-full bg-surface border border-hairline rounded-2xl rounded-tl-sm px-4 py-3.5">
        {!preview && (
          <div
            onDragOver={(e) => e.preventDefault()}
            onDrop={handleDrop}
            onClick={() => inputRef.current?.click()}
            className="border border-dashed border-hairline hover:border-brass rounded-xl py-6 flex flex-col items-center justify-center gap-2 cursor-pointer transition-colors"
          >
            <Upload size={18} className="text-muted" />
            <p className="text-xs text-muted">Drop a photo, or tap to choose one</p>
            <input
              ref={inputRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => handleFile(e.target.files?.[0])}
            />
          </div>
        )}

        {preview && (
          <div>
            <div className="relative w-24 h-24 rounded-lg overflow-hidden mb-3">
              <img src={preview} alt="Uploaded reference" className="w-full h-full object-cover" />
              {!loading && (
                <button
                  onClick={() => { setPreview(null); setResults(null); setError(null) }}
                  className="absolute top-1 right-1 w-5 h-5 rounded-full bg-ink/80 flex items-center justify-center"
                  aria-label="Remove photo"
                >
                  <X size={11} className="text-ivory" />
                </button>
              )}
            </div>

            {loading && (
              <p className="text-xs text-muted font-mono flex items-center gap-1.5">
                <Loader2 size={12} className="animate-spin" /> Looking for similar pieces…
              </p>
            )}

            {error && <p className="text-xs text-garnet">{error}</p>}

            {results && (
              <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                <p className="text-xs text-muted mb-1">Closest matches from the catalogue:</p>
                <ProductGrid products={results} sessionId={sessionId} />
              </motion.div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
