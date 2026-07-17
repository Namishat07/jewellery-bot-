import { motion } from 'framer-motion'
import { Gem, User } from 'lucide-react'
import ProductGrid from './ProductGrid'

export default function MessageBubble({ message, sessionId }) {
  const isUser = message.role === 'user'

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className={`flex gap-3 ${isUser ? 'flex-row-reverse' : 'flex-row'}`}
    >
      <div className="hallmark mt-0.5">
        {isUser ? (
          <User size={13} className="text-muted" />
        ) : (
          <Gem size={13} className="text-brass" />
        )}
      </div>

      <div className={`max-w-[85%] sm:max-w-[75%] ${isUser ? 'items-end' : 'items-start'} flex flex-col`}>
        <div
          className={`rounded-2xl px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap ${
            isUser
              ? 'bg-brass text-ink rounded-tr-sm'
              : 'bg-surface border border-hairline text-ivory rounded-tl-sm'
          }`}
        >
          {message.content}
          {message.streaming && (
            <span className="inline-block w-1.5 h-3.5 bg-current opacity-60 ml-0.5 animate-pulse align-middle" />
          )}
        </div>

        {message.products && (
          <div className="w-full mt-1">
            <ProductGrid products={message.products} sessionId={sessionId} />
          </div>
        )}
      </div>
    </motion.div>
  )
}
