import { useEffect, useRef, useState, useCallback } from 'react'
import { motion } from 'framer-motion'
import { X, AlertCircle, Loader2, Camera } from 'lucide-react'
import { prepareTryOn } from '../lib/tryonApi'

/**
 * TryOnView — full-screen camera try-on for a single product.
 *
 * Flow (see the "runtime flow" diagram this was scaffolded from):
 *   1. Ask the backend to prepare a background-removed cutout for this product.
 *   2. Ask the user for camera access.
 *   3. Load the right MediaPipe landmarker for this product's anchor type
 *      (face for earrings/necklaces, hand for rings/bangles).
 *   4. Loop: detect landmarks each frame -> position/scale the cutout -> draw.
 *   5. On close/unmount: stop the camera track and release the landmarker.
 *
 * All tracking runs on-device in the browser — nothing here calls the
 * backend after step 1.
 */

// Dynamic import so the ~mediapipe WASM/model payload only downloads when
// someone actually opens try-on, not on every page load.
async function loadVisionTasks() {
  return import('@mediapipe/tasks-vision')
}

const MODEL_URLS = {
  face: 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task',
  hand: 'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task',
}
const WASM_BASE = 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm'

// MediaPipe Face Mesh landmark indices used for anchoring earrings/necklaces.
const FACE = { leftEar: 234, rightEar: 454, jawLeft: 148, jawRight: 377 }
// MediaPipe Hand landmark indices used for anchoring rings/bangles.
const HAND = { wrist: 0, ringPip: 14 }

function isHandAnchor(anchorType) {
  return anchorType === 'ring' || anchorType === 'bangle'
}

export default function TryOnView({ product, sessionId, onClose }) {
  const videoRef = useRef(null)
  const canvasRef = useRef(null)
  const streamRef = useRef(null)
  const landmarkerRef = useRef(null)
  const overlayImgRef = useRef(null)
  const rafRef = useRef(null)

  // preparing -> permission -> tracking, or error at any point
  const [status, setStatus] = useState('preparing')
  const [error, setError] = useState(null)
  const [anchorType, setAnchorType] = useState(null)

  // Step 1 — prepare the cutout asset on the backend, preload it as an <img>.
  useEffect(() => {
    let cancelled = false
    async function prepare() {
      try {
        const { anchor_type, asset_url } = await prepareTryOn(sessionId, product.id)
        if (cancelled) return
        const img = new Image()
        img.crossOrigin = 'anonymous'
        img.onload = () => { overlayImgRef.current = img }
        img.src = asset_url
        setAnchorType(anchor_type)
        setStatus('permission')
      } catch (e) {
        if (!cancelled) {
          setError(e.message)
          setStatus('error')
        }
      }
    }
    prepare()
    return () => { cancelled = true }
  }, [product.id, sessionId])

  // Step 2/3 — request camera access, load the matching landmarker, start the loop.
  const startCamera = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'user', width: 640, height: 480 },
      })
      streamRef.current = stream
      videoRef.current.srcObject = stream
      await videoRef.current.play()

      const vision = await loadVisionTasks()
      const filesetResolver = await vision.FilesetResolver.forVisionTasks(WASM_BASE)
      const useHand = isHandAnchor(anchorType)

      landmarkerRef.current = useHand
        ? await vision.HandLandmarker.createFromOptions(filesetResolver, {
            baseOptions: { modelAssetPath: MODEL_URLS.hand },
            runningMode: 'VIDEO',
            numHands: 1,
          })
        : await vision.FaceLandmarker.createFromOptions(filesetResolver, {
            baseOptions: { modelAssetPath: MODEL_URLS.face },
            runningMode: 'VIDEO',
            numFaces: 1,
          })

      setStatus('tracking')
      trackLoop(useHand)
    } catch (e) {
      setError(
        e.name === 'NotAllowedError'
          ? 'Camera access was denied. Allow camera access in your browser settings to try this on.'
          : e.message || 'Could not start the camera.'
      )
      setStatus('error')
    }
  }, [anchorType])

  // Step 4 — the per-frame detect + draw loop.
  function trackLoop(useHand) {
    const video = videoRef.current
    const canvas = canvasRef.current
    if (!video || !canvas || !landmarkerRef.current) return
    const ctx = canvas.getContext('2d')

    const draw = () => {
      if (!videoRef.current || !landmarkerRef.current) return
      canvas.width = video.videoWidth
      canvas.height = video.videoHeight

      const now = performance.now()
      const result = landmarkerRef.current.detectForVideo(video, now)

      ctx.clearRect(0, 0, canvas.width, canvas.height)
      // Mirror the drawn video so it matches how people expect a "selfie" view to look.
      ctx.save()
      ctx.scale(-1, 1)
      ctx.translate(-canvas.width, 0)
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height)
      ctx.restore()

      const img = overlayImgRef.current
      if (img) {
        if (useHand && result.landmarks?.length) {
          drawOnHand(ctx, result.landmarks[0], img, canvas.width, canvas.height, anchorType)
        } else if (!useHand && result.faceLandmarks?.length) {
          drawOnFace(ctx, result.faceLandmarks[0], img, canvas.width, canvas.height, anchorType)
        }
      }

      rafRef.current = requestAnimationFrame(draw)
    }
    draw()
  }

  // Step 5 — cleanup: stop the camera and release the landmarker on unmount.
  useEffect(() => {
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
      streamRef.current?.getTracks().forEach((t) => t.stop())
      landmarkerRef.current?.close?.()
    }
  }, [])

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-50 bg-ink/95 flex flex-col items-center justify-center p-4"
    >
      <button
        onClick={onClose}
        className="absolute top-4 right-4 text-ivory/70 hover:text-brass transition-colors"
        aria-label="Close try-on"
      >
        <X size={24} />
      </button>

      <p className="text-ivory text-sm mb-3 text-center px-8 line-clamp-1">
        Trying on: <span className="text-brass">{product.title}</span>
      </p>

      <div className="relative w-full max-w-md aspect-[4/3] bg-surface rounded-xl overflow-hidden border border-hairline">
        <video ref={videoRef} className="hidden" playsInline muted />
        <canvas ref={canvasRef} className="w-full h-full object-cover" />

        {status === 'preparing' && (
          <Centered>
            <Loader2 size={28} className="animate-spin text-brass" />
            <p className="text-muted text-xs mt-2">Preparing this piece for try-on…</p>
          </Centered>
        )}

        {status === 'permission' && (
          <Centered>
            <Camera size={28} className="text-brass mb-2" />
            <p className="text-ivory text-sm mb-3 text-center px-6">
              Allow camera access to see how this looks on you
            </p>
            <button
              onClick={startCamera}
              className="bg-brass text-ink text-sm font-medium px-4 py-2 rounded-lg hover:bg-brassBright transition-colors"
            >
              Turn on camera
            </button>
          </Centered>
        )}

        {status === 'error' && (
          <Centered>
            <AlertCircle size={28} className="text-garnet mb-2" />
            <p className="text-ivory text-sm text-center px-6">{error}</p>
          </Centered>
        )}
      </div>

      <p className="text-muted text-xs mt-3 text-center px-8">
        Nothing here is uploaded or stored — the camera feed stays on your device.
      </p>
    </motion.div>
  )
}

function Centered({ children }) {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center">
      {children}
    </div>
  )
}

// --- Landmark-to-overlay positioning -----------------------------------

function drawOnFace(ctx, landmarks, img, w, h, anchorType) {
  const pt = (i) => ({ x: (1 - landmarks[i].x) * w, y: landmarks[i].y * h }) // mirrored x
  const leftEar = pt(FACE.leftEar)
  const rightEar = pt(FACE.rightEar)
  const faceWidth = Math.hypot(rightEar.x - leftEar.x, rightEar.y - leftEar.y)

  if (anchorType === 'earring') {
    const size = faceWidth * 0.28
    ;[leftEar, rightEar].forEach((p) => {
      ctx.drawImage(img, p.x - size / 2, p.y - size * 0.1, size, size)
    })
  } else {
    // necklace: anchor just below the jawline midpoint, scaled to face width
    const jawLeft = pt(FACE.jawLeft)
    const jawRight = pt(FACE.jawRight)
    const center = {
      x: (jawLeft.x + jawRight.x) / 2,
      y: (jawLeft.y + jawRight.y) / 2 + faceWidth * 0.55,
    }
    const size = faceWidth * 1.1
    ctx.drawImage(img, center.x - size / 2, center.y - size * 0.15, size, size)
  }
}

function drawOnHand(ctx, landmarks, img, w, h, anchorType) {
  const pt = (i) => ({ x: (1 - landmarks[i].x) * w, y: landmarks[i].y * h })
  const wrist = pt(HAND.wrist)
  const ringPip = pt(HAND.ringPip)
  const handScale = Math.hypot(ringPip.x - wrist.x, ringPip.y - wrist.y)

  const target = anchorType === 'ring' ? ringPip : wrist
  const size = anchorType === 'ring' ? handScale * 0.9 : handScale * 1.6
  ctx.drawImage(img, target.x - size / 2, target.y - size / 2, size, size)
}
