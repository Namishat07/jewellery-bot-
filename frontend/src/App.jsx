import { useState } from 'react'
import LandingScreen from './components/LandingScreen'
import ChatView from './components/ChatView'

export default function App() {
  const [session, setSession] = useState(null)

  if (!session) {
    return <LandingScreen onSessionCreated={setSession} />
  }

  return <ChatView session={session} />
}
