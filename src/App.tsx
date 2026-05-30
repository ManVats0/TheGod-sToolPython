import ResetPassword from './pages/ResetPassword'
import Pricing from './pages/Pricing'
import { useState, useEffect } from 'react'
import { Routes, Route, useLocation, useNavigate } from 'react-router-dom'
import Sidebar from './components/Sidebar'
import NotificationPanel from './components/NotificationPanel'
import DataSources from './pages/DataSources'
import DataCatalog from './pages/DataCatalog'
import Flow from './pages/Flow'
import Analysis from './pages/Analysis'
import AuditLogs from './pages/AuditLogs'
import { useStore } from './store'
import Documentation from './pages/Documentation'
import Tunneling from './pages/Tunneling'

export default function App() {
  const [showNotif, setShowNotif] = useState(false)
  const navigate = useNavigate()
  const [sidebarExpanded, setSidebarExpanded] = useState(true)
  const location = useLocation()

const [entered, setEntered] = useState(() => !!localStorage.getItem('datapro_logged_in'))
const [userPlan, setUserPlan] = useState<string>(() => {
  const stored = localStorage.getItem('datapro_plan')
  return stored || 'none'
})
const [showLogin, setShowLogin] = useState(false)
const [loginEmail, setLoginEmail] = useState('')
const [loginPassword, setLoginPassword] = useState('')
const [loginError, setLoginError] = useState('')
const [isRegisterMode, setIsRegisterMode] = useState(false)
const [showForgot, setShowForgot] = useState(false)
const [forgotEmail, setForgotEmail] = useState('')
const [forgotMsg, setForgotMsg] = useState('')
const [forgotError, setForgotError] = useState('')

  useEffect(() => {
    if (location.pathname !== '/docs') {
      localStorage.setItem('datapro_last_page', location.pathname)
    }
  }, [location.pathname]) // Default to true if you prefer it open
  const notifications = useStore(s => s.notifications)
  const readIds = useStore(s => s.readIds)
  const unread = notifications.filter(n => !readIds.has(n.id)).length

  const speakWelcome = () => {
    window.speechSynthesis.cancel()
    const utterance = new SpeechSynthesisUtterance('We are so happy you found this technology.')
    utterance.pitch = 0.1
    utterance.rate = 0.78
    utterance.volume = 1
    const trySpeak = () => {
      const voices = window.speechSynthesis.getVoices()
      const deep = voices.find(v => /male/i.test(v.name) || /david/i.test(v.name) || /daniel/i.test(v.name) || /james/i.test(v.name))
      if (deep) utterance.voice = deep
      window.speechSynthesis.speak(utterance)
    }
    if (window.speechSynthesis.getVoices().length > 0) {
      trySpeak()
    } else {
      window.speechSynthesis.onvoiceschanged = () => { trySpeak(); window.speechSynthesis.onvoiceschanged = null }
    }
  }

  useEffect(() => {
    if (entered) speakWelcome()
  }, [entered])

  useEffect(() => {
    const handler = (e: MessageEvent) => {
      if (e.data?.type === 'GOOGLE_TOKEN' && (window as any)._googleLoginCallback) {
        (window as any)._googleLoginCallback(e.data.token)
      }
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [])

  const [showIndra, setShowIndra] = useState(true)
  const handleEnter = () => setShowLogin(true)

  const handleLogin = async (e?: React.FormEvent) => {
    e?.preventDefault()
    if (!loginEmail.trim() || !loginPassword.trim()) {
      setLoginError('Please enter email and password.')
      return
    }
    try {
      const endpoint = isRegisterMode ? 'register' : 'login'
      const res = await fetch(`${import.meta.env.VITE_AUTH_URL}/api/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: loginEmail, password: loginPassword })
      })
      const data = await res.json()
      if (!res.ok) {
        setLoginError(data.error || (isRegisterMode ? 'Registration failed' : 'Login failed'))
        return
      }
      localStorage.setItem('datapro_logged_in', '1')
      localStorage.setItem('datapro_token', data.token)
      localStorage.setItem('datapro_email', data.email)
      // Founder bypass
      if (data.email === 'vatsaman888@gmail.com') {
        localStorage.setItem('datapro_plan', 'pro_yearly')
        setUserPlan('pro_yearly')
        setEntered(true)
        setShowLogin(false)
        return
      }
      // Fetch plan
      try {
        const planRes = await fetch('${import.meta.env.VITE_AUTH_URL}/api/plan', {
          headers: { Authorization: `Bearer ${data.token}` }
        })
        const planData = await planRes.json()
        const plan = planData.plan || 'none'
        localStorage.setItem('datapro_plan', plan)
        setUserPlan(plan)
      } catch { /* ignore */ }
      setEntered(true)
      setShowLogin(false)
    } catch (err) {
      setLoginError('Cannot connect to server. Make sure backend is running.')
    }
  }

  const handleForgotPassword = async () => {
    if (!forgotEmail.trim()) { setForgotError('Please enter your email.'); return }
    try {
      const res = await fetch('${import.meta.env.VITE_AUTH_URL}/api/forgot-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: forgotEmail })
      })
      const data = await res.json()
      if (!res.ok) { setForgotError(data.error || 'Failed'); return }
      setForgotMsg('Reset link sent! Check your email.')
      setForgotError('')
    } catch {
      setForgotError('Cannot connect to server.')
    }
  }

 const handleGoogleLogin = () => {
    const clientId = '973595012454-ojh9i31u6ocq5l12t8gccer8dlc8gaim.apps.googleusercontent.com'
    ;(window as any).google.accounts.id.initialize({
      client_id: clientId,
      callback: async (response: any) => {
        try {
          const payload = JSON.parse(atob(response.credential.split('.')[1]))
          const backendRes = await fetch('${import.meta.env.VITE_AUTH_URL}/api/google', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              email: payload.email,
              googleId: payload.sub,
              name: payload.name
            })
          })
          const data = await backendRes.json()
          if (!backendRes.ok) { setLoginError(data.error || 'Google login failed'); return }
          localStorage.setItem('datapro_logged_in', '1')
          localStorage.setItem('datapro_token', data.token)
          localStorage.setItem('datapro_email', data.email)
          try {
            const planRes = await fetch('${import.meta.env.VITE_AUTH_URL}/api/plan', {
              headers: { Authorization: `Bearer ${data.token}` }
            })
            const planData = await planRes.json()
            const plan = planData.plan || 'none'
            localStorage.setItem('datapro_plan', plan)
            setUserPlan(plan)
          } catch { /* ignore */ }
          setEntered(true)
          setShowLogin(false)
        } catch {
          setLoginError('Google login failed. Try again.')
        }
      }
    })
    ;(window as any).google.accounts.id.prompt()
  }

  if (location.pathname === '/reset-password') return (
    <Routes>
      <Route path="/reset-password" element={<ResetPassword />} />
    </Routes>
  )

  if (location.pathname === '/pricing') return (
    <Routes>
      <Route path="/pricing" element={<Pricing />} />
    </Routes>
  )

if (!entered) return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', flexDirection: 'column', gap: '24px', position: 'relative', overflow: 'hidden',
      background: '#0a0a1a',
    }}>
      {/* Math grid background */}
      <svg style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', opacity: 0.13, pointerEvents: 'none' }} xmlns="http://www.w3.org/2000/svg">
        <defs>
          <pattern id="smallGrid" width="30" height="30" patternUnits="userSpaceOnUse">
            <path d="M 30 0 L 0 0 0 30" fill="none" stroke="#7c6fff" strokeWidth="0.5"/>
          </pattern>
          <pattern id="grid" width="150" height="150" patternUnits="userSpaceOnUse">
            <rect width="150" height="150" fill="url(#smallGrid)"/>
            <path d="M 150 0 L 0 0 0 150" fill="none" stroke="#a78bfa" strokeWidth="1.2"/>
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#grid)" />
      </svg>

      {/* Floating math symbols */}
      {[
        { t: '∞', x: '8%', y: '12%', s: '2.8rem', o: 0.18 },
        { t: 'Σ', x: '88%', y: '8%', s: '3rem', o: 0.15 },
        { t: 'π', x: '5%', y: '78%', s: '2.4rem', o: 0.18 },
        { t: '∇', x: '91%', y: '80%', s: '2.6rem', o: 0.15 },
        { t: '∂', x: '50%', y: '6%', s: '2rem', o: 0.12 },
        { t: 'Ω', x: '22%', y: '90%', s: '2.2rem', o: 0.13 },
        { t: '√', x: '75%', y: '88%', s: '2.6rem', o: 0.14 },
        { t: 'λ', x: '15%', y: '45%', s: '2rem', o: 0.11 },
        { t: '∫', x: '85%', y: '42%', s: '2.8rem', o: 0.13 },
        { t: 'φ', x: '60%', y: '15%', s: '2rem', o: 0.12 },
        { t: 'Δ', x: '38%', y: '82%', s: '2.2rem', o: 0.13 },
        { t: '⊗', x: '70%', y: '55%', s: '1.8rem', o: 0.10 },
      ].map((s, i) => (
        <div key={i} style={{ position: 'absolute', left: s.x, top: s.y, fontSize: s.s, color: '#c4b5fd', opacity: s.o, fontFamily: 'Georgia, serif', pointerEvents: 'none', userSelect: 'none' }}>{s.t}</div>
      ))}

      {/* Glowing orb behind title */}
      <div style={{ position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%, -60%)', width: '420px', height: '420px', borderRadius: '50%', background: 'radial-gradient(circle, rgba(124,58,237,0.18) 0%, rgba(79,70,229,0.08) 50%, transparent 75%)', pointerEvents: 'none' }} />

      {/* Indra popup */}
      {showIndra && (
        <div style={{
          position: 'fixed', bottom: '32px', right: '32px', zIndex: 999,
          width: '320px',
          background: 'linear-gradient(145deg, #0a0820 0%, #110d2e 60%, #0d1a3a 100%)',
          border: '1px solid rgba(99,179,237,0.25)',
          borderRadius: '12px',
          boxShadow: '0 0 40px rgba(66,153,225,0.15), 0 8px 32px rgba(0,0,0,0.6)',
          overflow: 'hidden',
          animation: 'indraSlide 0.5s cubic-bezier(0.16,1,0.3,1)',
        }}>
          <style>{`
            @keyframes indraSlide {
              from { opacity: 0; transform: translateY(24px) scale(0.97); }
              to   { opacity: 1; transform: translateY(0)    scale(1);    }
            }
            @keyframes indraLightning {
              0%,100% { opacity: 0.5; }
              50%      { opacity: 1;   }
            }
          `}</style>

          {/* Image strip */}
          <div style={{ position: 'relative', height: '240px', overflow: 'hidden' }}>
            <img
              src="https://i.postimg.cc/DfcCXhSJ/Indra.png"
              alt="Indra"
              style={{ width: '100%', height: '100%', objectFit: 'cover', objectPosition: 'center 20%', filter: 'brightness(0.75) saturate(1.4) contrast(1.1)' }}
              onError={e => { (e.currentTarget as HTMLImageElement).style.display = 'none' }}
            />
            {/* Blue lightning overlay */}
            <div style={{ position: 'absolute', inset: 0, background: 'linear-gradient(180deg, transparent 40%, #0a0820 100%)' }} />
            <div style={{ position: 'absolute', inset: 0, background: 'radial-gradient(ellipse at center, rgba(66,153,225,0.18) 0%, transparent 70%)', animation: 'indraLightning 2.5s ease-in-out infinite' }} />
            {/* Name badge */}
            <div style={{ position: 'absolute', bottom: '8px', left: '14px', display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span style={{ fontSize: '1.1rem' }}>⚡</span>
              <span style={{ fontFamily: 'Georgia, serif', color: '#90cdf4', fontSize: '1rem', fontWeight: 700, letterSpacing: '0.15em', textShadow: '0 0 12px rgba(66,153,225,0.8)' }}>INDRA</span>
              <span style={{ fontFamily: 'Georgia, serif', color: '#63b3ed', fontSize: '0.65rem', opacity: 0.7, letterSpacing: '0.1em' }}>GOD OF RAIN & THUNDER</span>
            </div>
          </div>

          {/* Text body */}
          <div style={{ padding: '14px 16px 16px' }}>
            <p style={{
              fontFamily: 'Georgia, serif',
              fontSize: '0.82rem',
              lineHeight: 1.7,
              color: '#a0c4e8',
              margin: 0,
              fontStyle: 'italic',
            }}>
              "I used this tool in the 1990s to identify vulnerable areas that needed rain. Now — due to the sins of people — I follow no rules. To rain or not to rain... is simply my choice."
            </p>

            <div style={{ marginTop: '12px', display: 'flex', justifyContent: 'flex-end' }}>
              <button
                onClick={() => setShowIndra(false)}
                style={{
                  fontFamily: 'Georgia, serif',
                  background: 'transparent',
                  border: '1px solid rgba(99,179,237,0.3)',
                  color: '#63b3ed',
                  padding: '5px 16px',
                  borderRadius: '4px',
                  fontSize: '0.75rem',
                  letterSpacing: '0.1em',
                  cursor: 'pointer',
                }}
              >
                Understood
              </button>
            </div>
          </div>
        </div>
      )}

      {!showLogin ? (
        <div style={{ position: 'relative', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '32px' }}>
          {/* Crown / deity symbol */}
          <div style={{ fontSize: '3.5rem', lineHeight: 1, filter: 'drop-shadow(0 0 24px #a78bfa88)' }}>♔</div>

          <div style={{ textAlign: 'center' }}>
            <div style={{
              fontFamily: '"Palatino Linotype", Palatino, Georgia, serif',
              fontSize: '3.6rem',
              fontWeight: 900,
              letterSpacing: '0.08em',
              lineHeight: 1.1,
              background: 'linear-gradient(135deg, #e9d5ff 0%, #a78bfa 40%, #7c3aed 70%, #c4b5fd 100%)',
              WebkitBackgroundClip: 'text',
              WebkitTextFillColor: 'transparent',
              backgroundClip: 'text',
              filter: 'drop-shadow(0 0 32px rgba(167,139,250,0.5))',
            }}>The God's Tool</div>
            <div style={{
              fontFamily: 'Georgia, serif',
              fontSize: '0.95rem',
              color: '#7c6fff',
              letterSpacing: '0.35em',
              marginTop: '10px',
              opacity: 0.7,
              textTransform: 'uppercase',
            }}>Omniscience · Precision · Power</div>
          </div>

          {/* Decorative math line */}
          <div style={{ fontFamily: 'Georgia, serif', color: '#6d5fc7', fontSize: '1rem', opacity: 0.5, letterSpacing: '0.1em' }}>
            E = mc² &nbsp;·&nbsp; i² = −1 &nbsp;·&nbsp; e<sup>iπ</sup> + 1 = 0
          </div>

          <button
            onClick={handleEnter}
            style={{
              fontFamily: '"Palatino Linotype", Georgia, serif',
              background: 'linear-gradient(135deg, #7c3aed 0%, #4f46e5 100%)',
              color: 'white',
              border: '1px solid rgba(167,139,250,0.4)',
              padding: '16px 64px',
              borderRadius: '4px',
              fontSize: '1.1rem',
              fontWeight: 700,
              letterSpacing: '0.18em',
              cursor: 'pointer',
              boxShadow: '0 0 32px rgba(124,58,237,0.45), inset 0 1px 0 rgba(255,255,255,0.1)',
              textTransform: 'uppercase',
              transition: 'box-shadow 0.2s',
            }}
            onMouseEnter={e => (e.currentTarget.style.boxShadow = '0 0 56px rgba(124,58,237,0.75), inset 0 1px 0 rgba(255,255,255,0.15)')}
            onMouseLeave={e => (e.currentTarget.style.boxShadow = '0 0 32px rgba(124,58,237,0.45), inset 0 1px 0 rgba(255,255,255,0.1)')}
          >
            Enter
          </button>

          {/* Bottom formula */}
          <div style={{ fontFamily: 'Georgia, serif', color: '#4f3fa0', fontSize: '0.85rem', opacity: 0.45, letterSpacing: '0.08em' }}>
            ∇²ψ + k²ψ = 0 &nbsp;&nbsp; ∮ B · dA = 0
          </div>
        </div>
      ) : (
        <div style={{ position: 'relative', minHeight: '100vh', width: '100vw', display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#05080f', overflow: 'hidden' }}>
          <style>{`
            @keyframes pulse-dot { 0%,100%{opacity:0.15} 50%{opacity:0.45} }
            @keyframes float-up { 0%{transform:translateY(0);opacity:0.18} 100%{transform:translateY(-80px);opacity:0} }
            .auth-input {
              width: 100%;
              padding: 13px 16px;
              background: rgba(255,255,255,0.04);
              border: 1px solid rgba(99,179,237,0.18);
              border-radius: 6px;
              color: #e2e8f0;
              font-size: 0.95rem;
              font-family: 'Georgia', serif;
              box-sizing: border-box;
              outline: none;
              transition: border-color 0.2s;
            }
            .auth-input::placeholder { color: rgba(148,163,184,0.5); }
            .auth-input:focus { border-color: rgba(99,179,237,0.55); }
            .auth-btn-primary {
              width: 100%;
              padding: 14px;
              background: linear-gradient(135deg, #1a56db 0%, #1e40af 100%);
              border: none;
              border-radius: 6px;
              color: white;
              font-weight: 700;
              font-size: 0.95rem;
              letter-spacing: 0.08em;
              cursor: pointer;
              font-family: 'Georgia', serif;
              transition: opacity 0.2s;
            }
            .auth-btn-primary:hover { opacity: 0.88; }
            .auth-btn-google {
              display: flex;
              align-items: center;
              gap: 10px;
              background: rgba(255,255,255,0.06);
              border: 1px solid rgba(255,255,255,0.12);
              border-radius: 6px;
              padding: 12px 20px;
              font-size: 0.92rem;
              font-weight: 500;
              cursor: pointer;
              width: 100%;
              justify-content: center;
              color: #cbd5e1;
              font-family: 'Georgia', serif;
              transition: background 0.2s;
            }
            .auth-btn-google:hover { background: rgba(255,255,255,0.1); }
          `}</style>

          {/* Animated dot grid */}
          <svg style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }} xmlns="http://www.w3.org/2000/svg">
            <defs>
              <pattern id="dots" width="28" height="28" patternUnits="userSpaceOnUse">
                <circle cx="1.5" cy="1.5" r="1" fill="#334155" opacity="0.5"/>
              </pattern>
            </defs>
            <rect width="100%" height="100%" fill="url(#dots)" />
          </svg>

          {/* Floating data particles */}
          {['SELECT *', 'JOIN', 'GROUP BY', 'WHERE', 'AVG()', 'COUNT(*)', 'HAVING', 'UNION', 'INDEX', 'SCHEMA'].map((word, i) => (
            <div key={i} style={{
              position: 'absolute',
              left: `${8 + (i * 9.2) % 85}%`,
              top: `${60 + (i * 13) % 35}%`,
              fontFamily: 'monospace',
              fontSize: '0.72rem',
              color: '#38bdf8',
              opacity: 0.12,
              pointerEvents: 'none',
              animation: `float-up ${3 + (i % 3)}s ease-in ${i * 0.6}s infinite`,
              userSelect: 'none',
            }}>{word}</div>
          ))}

          {/* Glow accent top-left */}
          <div style={{ position: 'absolute', top: '-80px', left: '-80px', width: '400px', height: '400px', borderRadius: '50%', background: 'radial-gradient(circle, rgba(30,64,175,0.2) 0%, transparent 70%)', pointerEvents: 'none' }} />
          {/* Glow accent bottom-right */}
          <div style={{ position: 'absolute', bottom: '-100px', right: '-60px', width: '350px', height: '350px', borderRadius: '50%', background: 'radial-gradient(circle, rgba(14,116,144,0.18) 0%, transparent 70%)', pointerEvents: 'none' }} />

          {/* Card */}
          <div style={{ position: 'relative', zIndex: 2, width: '100%', maxWidth: '400px', margin: '0 16px', background: 'rgba(10,15,28,0.85)', border: '1px solid rgba(99,179,237,0.15)', borderRadius: '12px', padding: '36px 32px', backdropFilter: 'blur(12px)', display: 'flex', flexDirection: 'column', gap: '18px' }}>

            {/* Header */}
            <div style={{ textAlign: 'center', marginBottom: '4px' }}>
              <div style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center', width: '48px', height: '48px', borderRadius: '10px', background: 'rgba(30,64,175,0.25)', border: '1px solid rgba(99,179,237,0.2)', marginBottom: '14px' }}>
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#60a5fa" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3"/>
                </svg>
              </div>
              <div style={{ fontFamily: '"Palatino Linotype", Georgia, serif', fontSize: '1.5rem', fontWeight: 700, color: '#e2e8f0', letterSpacing: '0.04em', lineHeight: 1.2 }}>The God's Tool</div>
              <div style={{ fontFamily: 'monospace', fontSize: '0.72rem', color: '#38bdf8', letterSpacing: '0.2em', marginTop: '6px', opacity: 0.7 }}>DATA · INTELLIGENCE · PIPELINE</div>
            </div>

            {/* Stat pills */}
            <div style={{ display: 'flex', gap: '8px', justifyContent: 'center', flexWrap: 'wrap' }}>
              {[['∞', 'Data Sources'], ['⚡', 'Real-time'], ['🔒', 'Encrypted']].map(([icon, label]) => (
                <div key={label} style={{ display: 'flex', alignItems: 'center', gap: '5px', background: 'rgba(30,64,175,0.15)', border: '1px solid rgba(99,179,237,0.15)', borderRadius: '20px', padding: '4px 12px', fontSize: '0.72rem', color: '#94a3b8', fontFamily: 'monospace' }}>
                  <span style={{ fontSize: '0.8rem' }}>{icon}</span>{label}
                </div>
              ))}
            </div>

            {/* Divider */}
            <div style={{ borderTop: '1px solid rgba(99,179,237,0.1)', margin: '0 -4px' }} />

            {/* Google button */}
            <button onClick={handleGoogleLogin} className="auth-btn-google">
              <svg width="18" height="18" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.18 1.48-4.97 2.31-8.16 2.31-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>
              Continue with Google
            </button>

            {/* OR divider */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <div style={{ flex: 1, height: '1px', background: 'rgba(99,179,237,0.12)' }} />
              <span style={{ color: '#475569', fontSize: '0.78rem', fontFamily: 'monospace' }}>OR</span>
              <div style={{ flex: 1, height: '1px', background: 'rgba(99,179,237,0.12)' }} />
            </div>

            {/* Inputs */}
            <input
              type="email"
              placeholder="you@example.com"
              value={loginEmail}
              onChange={e => setLoginEmail(e.target.value)}
              className="auth-input"
            />
            <input
              type="password"
              placeholder="Password"
              value={loginPassword}
              onChange={e => setLoginPassword(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleLogin()}
              className="auth-input"
            />

            {loginError && (
              <div style={{ background: 'rgba(220,38,38,0.1)', border: '1px solid rgba(220,38,38,0.25)', borderRadius: '6px', padding: '10px 14px', color: '#fca5a5', fontSize: '0.82rem', fontFamily: 'monospace' }}>
                ⚠ {loginError}
              </div>
            )}

            {/* Submit */}
            <button onClick={() => handleLogin()} className="auth-btn-primary">
              {isRegisterMode ? 'CREATE ACCOUNT' : 'ACCESS THE TOOL'}
            </button>

            {/* Forgot password */}
            {!isRegisterMode && !showForgot && (
              <div style={{ textAlign: 'center' }}>
                <span onClick={() => { setShowForgot(true); setForgotMsg(''); setForgotError('') }} style={{ color: '#38bdf8', fontSize: '0.78rem', fontFamily: 'monospace', cursor: 'pointer', textDecoration: 'underline' }}>Forgot password?</span>
              </div>
            )}

            {/* Forgot password form */}
            {showForgot && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', background: 'rgba(30,64,175,0.08)', border: '1px solid rgba(99,179,237,0.15)', borderRadius: '8px', padding: '16px' }}>
                <div style={{ color: '#94a3b8', fontSize: '0.8rem', fontFamily: 'monospace' }}>Enter your email to receive a reset link:</div>
                <input
                  type="email"
                  placeholder="you@example.com"
                  value={forgotEmail}
                  onChange={e => setForgotEmail(e.target.value)}
                  className="auth-input"
                />
                {forgotError && <div style={{ color: '#fca5a5', fontSize: '0.78rem', fontFamily: 'monospace' }}>⚠ {forgotError}</div>}
                {forgotMsg && <div style={{ color: '#86efac', fontSize: '0.78rem', fontFamily: 'monospace' }}>✓ {forgotMsg}</div>}
                <div style={{ display: 'flex', gap: '8px' }}>
                  <button onClick={handleForgotPassword} className="auth-btn-primary" style={{ flex: 1, padding: '10px' }}>Send Reset Link</button>
                  <button onClick={() => setShowForgot(false)} style={{ flex: 1, padding: '10px', background: 'transparent', border: '1px solid rgba(99,179,237,0.2)', borderRadius: '6px', color: '#94a3b8', cursor: 'pointer', fontFamily: 'monospace', fontSize: '0.85rem' }}>Cancel</button>
                </div>
              </div>
            )}

            {/* Footer */}
            <div style={{ textAlign: 'center' }}>
              {isRegisterMode ? (
                <>
                  <span style={{ color: '#475569', fontSize: '0.78rem', fontFamily: 'monospace' }}>Already have an account? </span>
                  <span onClick={() => { setIsRegisterMode(false); setLoginError('') }} style={{ color: '#38bdf8', fontSize: '0.78rem', fontFamily: 'monospace', cursor: 'pointer', textDecoration: 'underline' }}>Sign in</span>
                </>
              ) : (
                <>
                  <span style={{ color: '#475569', fontSize: '0.78rem', fontFamily: 'monospace' }}>Don't have an account? </span>
                  <span onClick={() => { setIsRegisterMode(true); setLoginError('') }} style={{ color: '#38bdf8', fontSize: '0.78rem', fontFamily: 'monospace', cursor: 'pointer', textDecoration: 'underline' }}>Register</span>
                </>
              )}
            </div>

            <p style={{ color: '#334155', fontSize: '0.72rem', textAlign: 'center', margin: 0, fontFamily: 'monospace', lineHeight: 1.6 }}>
              By continuing you agree to The God's Tool's Terms of Service and Privacy Policy.
            </p>
          </div>
        </div>
      )}
    </div>
  )

  if (entered && userPlan === 'none') return (
    <div style={{ minHeight: '100vh', background: '#07050f', display: 'flex', alignItems: 'center', justifyContent: 'center', flexDirection: 'column', gap: '24px', padding: '24px' }}>
      <div style={{ textAlign: 'center', maxWidth: '480px' }}>
        <div style={{ fontSize: '3rem', marginBottom: '16px' }}>♔</div>
        <div style={{ fontFamily: 'Georgia, serif', fontSize: '1.8rem', fontWeight: 700, color: 'white', marginBottom: '8px' }}>Welcome to The God's Tool</div>
        <div style={{ color: 'rgba(255,255,255,0.4)', fontFamily: 'monospace', fontSize: '0.85rem', marginBottom: '32px' }}>Get started for free or unlock the full platform</div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', marginBottom: '24px' }}>
          {/* Free tier */}
          <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '12px', padding: '20px 24px', textAlign: 'left' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' }}>
              <span style={{ color: 'white', fontWeight: 700, fontFamily: 'Georgia, serif', fontSize: '1rem' }}>Free</span>
              <span style={{ color: '#4ade80', fontFamily: 'monospace', fontSize: '0.78rem', background: 'rgba(74,222,128,0.1)', padding: '2px 10px', borderRadius: '20px', border: '1px solid rgba(74,222,128,0.2)' }}>No credit card</span>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '16px' }}>
              {['✓ CSV & Google Sheets upload', '✓ Data analysis & cleaning', '✗ Other data sources (locked)', '✗ AI features (locked)'].map(f => (
                <div key={f} style={{ fontFamily: 'monospace', fontSize: '0.78rem', color: f.startsWith('✗') ? 'rgba(255,255,255,0.25)' : 'rgba(255,255,255,0.65)' }}>{f}</div>
              ))}
            </div>
            <button
              onClick={() => { localStorage.setItem('datapro_plan', 'free'); setUserPlan('free'); }}
              style={{ width: '100%', padding: '10px', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.12)', background: 'rgba(255,255,255,0.05)', color: 'white', fontWeight: 600, fontSize: '0.88rem', cursor: 'pointer', fontFamily: 'Georgia, serif' }}
            >
              Continue Free
            </button>
          </div>

          {/* Paid tier */}
          <div style={{ background: 'rgba(124,58,237,0.1)', border: '2px solid rgba(124,58,237,0.4)', borderRadius: '12px', padding: '20px 24px', textAlign: 'left' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' }}>
              <span style={{ color: 'white', fontWeight: 700, fontFamily: 'Georgia, serif', fontSize: '1rem' }}>Paid Plans</span>
              <span style={{ color: '#a78bfa', fontFamily: 'monospace', fontSize: '0.78rem' }}>from $9/mo</span>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '16px' }}>
              {['✓ All data sources (MySQL, Postgres, BigQuery…)', '✓ AI-powered data cleaning', '✓ Unlimited pipeline automation', '✓ Priority support'].map(f => (
                <div key={f} style={{ fontFamily: 'monospace', fontSize: '0.78rem', color: 'rgba(255,255,255,0.65)' }}>{f}</div>
              ))}
            </div>
            <button
              onClick={() => navigate('/pricing')}
              style={{ width: '100%', padding: '10px', borderRadius: '8px', border: 'none', background: 'linear-gradient(135deg, #7c3aed, #4f46e5)', color: 'white', fontWeight: 700, fontSize: '0.88rem', cursor: 'pointer', fontFamily: 'Georgia, serif' }}
            >
              View Plans →
            </button>
          </div>
        </div>

        <span onClick={() => { localStorage.removeItem('datapro_logged_in'); localStorage.removeItem('datapro_plan'); setUserPlan('none'); setEntered(false); setShowLogin(false); }} style={{ color: '#475569', fontFamily: 'monospace', fontSize: '0.78rem', cursor: 'pointer', textDecoration: 'underline' }}>
          Sign out
        </span>
      </div>
    </div>
  )

  return (
    <div className="flex min-h-screen" style={{ background: '#07050f' }}>
      <Sidebar 
  onOpenNotif={() => setShowNotif(true)} 
  unread={unread} 
  isExpanded={sidebarExpanded}
  onToggleExpand={() => setSidebarExpanded(!sidebarExpanded)}
  onLogout={() => { localStorage.removeItem('datapro_logged_in'); localStorage.removeItem('datapro_plan'); setUserPlan('none'); setEntered(false); setShowLogin(false); }}
/>
      
      <main className={`flex-1 min-h-screen overflow-auto transition-all duration-300 ${sidebarExpanded ? 'ml-64' : 'ml-16'}`}>
        {/* The floating hamburger menu that used to be here has been removed and moved inside the Sidebar */}
        
        <Routes>
          <Route path="/" element={<DataSources />} />
          <Route path="/sources" element={<DataSources />} />
          <Route path="/catalog" element={<DataCatalog />} />
          <Route path="/flow" element={<Flow />} />
          <Route path="/analysis" element={<Analysis />} />
          <Route path="/audit" element={<AuditLogs />} />
          <Route path="/docs" element={<Documentation />} />
          <Route path="/tunneling" element={<Tunneling />} />
          <Route path="/reset-password" element={<ResetPassword />} />
          <Route path="/pricing" element={<Pricing />} />
        </Routes>
      </main>
      
      {showNotif && <NotificationPanel onClose={() => setShowNotif(false)} />}
    </div>
  )
}