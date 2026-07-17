import { useState, useRef, useEffect } from 'react'
import { motion } from 'framer-motion'
import { Send, ImagePlus, SlidersHorizontal, Gem } from 'lucide-react'
import { streamChat } from '../lib/api'
import MessageBubble from './MessageBubble'
import FilterFlow from '../../FilterFlow'
import ImageUploadBubble from './ImageUploadBubble'

let idCounter = 0
const nextId = () => `m${idCounter++}`

export default function ChatView({ session }) {
  const [items, setItems] = useState([
    {
      id: nextId(),
      kind: 'message',
      role: 'assistant',
      content: `I've read through this site — ${session.product_count} pieces in the catalogue. Ask me anything about the products or policies, or use the buttons below for guided help.`,
    },
  ])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const scrollRef = useRef(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [items])

  async function handleSend(e) {
    e.preventDefault()
    const text = input.trim()
    if (!text || sending) return

    const userMsg = { id: nextId(), kind: 'message', role: 'user', content: text }
    const assistantId = nextId()
    setItems((prev) => [
      ...prev,
      userMsg,
      { id: assistantId, kind: 'message', role: 'assistant', content: '', streaming: true },
    ])
    setInput('')
    setSending(true)

    try {
      await streamChat(
        session.session_id,
        text,
        (chunk) => {
          setItems((prev) =>
            prev.map((it) =>
              it.id === assistantId ? { ...it, content: it.content + chunk } : it
            )
          )
        },
        () => {
          setItems((prev) =>
            prev.map((it) => (it.id === assistantId ? { ...it, streaming: false } : it))
          )
        }
      )
    } catch {
      setItems((prev) =>
        prev.map((it) =>
          it.id === assistantId
            ? { ...it, content: "I couldn't reach the assistant just now — try again.", streaming: false }
            : it
        )
      )
    } finally {
      setSending(false)
    }
  }

  function addFilterFlow() {
    setItems((prev) => [...prev, { id: nextId(), kind: 'filter' }])
  }

  function addImageUpload() {
    setItems((prev) => [...prev, { id: nextId(), kind: 'image-upload' }])
  }

  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-hairline px-5 py-3.5 flex items-center gap-2.5 sticky top-0 bg-ink/95 backdrop-blur z-10">
        <div className="hallmark">
          <Gem size={13} className="text-brass" />
        </div>
        <div>
          <p className="text-sm text-ivory font-medium leading-tight">{session.site_url}</p>
          <p className="text-[11px] text-muted font-mono">{session.product_count} pieces catalogued</p>
        </div>
      </header>

      <div ref={scrollRef} className="flex-1 overflow-y-auto px-5 py-6 space-y-5 max-w-3xl w-full mx-auto">
        {items.map((item) => {
          if (item.kind === 'message') return <MessageBubble key={item.id} message={item} sessionId={session.session_id} />
          if (item.kind === 'filter') return <FilterFlow key={item.id} sessionId={session.session_id} />
          if (item.kind === 'image-upload') return <ImageUploadBubble key={item.id} sessionId={session.session_id} />
          return null
        })}
      </div>

      <div className="border-t border-hairline px-5 py-4 sticky bottom-0 bg-ink/95 backdrop-blur">
        <div className="max-w-3xl w-full mx-auto">
          <div className="flex gap-2 mb-3">
            <button
              onClick={addImageUpload}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-full border border-hairline text-muted hover:border-brass hover:text-brass transition-colors"
            >
              <ImagePlus size={13} /> Search by photo
            </button>
            <button
              onClick={addFilterFlow}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-full border border-hairline text-muted hover:border-brass hover:text-brass transition-colors"
            >
              <SlidersHorizontal size={13} /> Guided recommendation
            </button>
          </div>

          <form onSubmit={handleSend} className="flex items-center gap-2 bg-surface border border-hairline rounded-full px-2 py-1.5 focus-within:border-brass transition-colors">
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Ask about a piece, or the return policy…"
              className="flex-1 bg-transparent px-3 py-2 text-sm text-ivory placeholder:text-muted/60 outline-none"
            />
            <motion.button
              type="submit"
              whileTap={{ scale: 0.92 }}
              disabled={!input.trim() || sending}
              className="flex items-center justify-center w-9 h-9 rounded-full bg-brass text-ink disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
              aria-label="Send message"
            >
              <Send size={15} />
            </motion.button>
          </form>
        </div>
      </div>
    </div>
  )
}
