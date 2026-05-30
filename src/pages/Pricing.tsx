import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

const PLANS = [
  {
    key: 'solo',
    name: 'Solo',
    price: '₹749',
    period: '/month',
    ai: false,
    aiLabel: 'No AI access',
    features: ['All data sources', 'Full pipeline builder', 'CSV cleaning', 'SQL translator'],
    color: '#6366f1',
    bg: 'rgba(99,102,241,0.08)',
    border: 'rgba(99,102,241,0.25)',
  },
  {
    key: 'starter',
    name: 'Starter',
    price: '₹12,470',
    period: '/year',
    ai: true,
    aiLabel: '⚡ 500 AI transforms/day',
    features: ['Everything in Solo', '500 AI transforms/day', 'Email support'],
    color: '#22d3ee',
    bg: 'rgba(34,211,238,0.08)',
    border: 'rgba(34,211,238,0.25)',
    badge: 'Best Value',
  },
  {
    key: 'pro_monthly',
    name: 'Pro',
    price: '₹1,579',
    period: '/month',
    ai: true,
    aiLabel: '⚡ 5,000 AI transforms/day',
    features: ['Everything in Starter', '5,000 AI transforms/day', 'Priority support'],
    color: '#a78bfa',
    bg: 'rgba(124,58,237,0.12)',
    border: 'rgba(124,58,237,0.4)',
    highlight: true,
  },
  {
    key: 'pro_yearly',
    name: 'Pro Yearly',
    price: '₹15,790',
    period: '/year',
    ai: true,
    aiLabel: '⚡ 10,000 AI transforms/day',
    features: ['Everything in Pro', '10,000 AI transforms/day', 'Priority support'],
    color: '#34d399',
    bg: 'rgba(52,211,153,0.08)',
    border: 'rgba(52,211,153,0.25)',
  },
]

export default function Pricing() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState<string | null>(null)
  const [error, setError] = useState('')

  const handleSubscribe = async (planKey: string) => {
    setError('')
    setLoading(planKey)
    const token = localStorage.getItem('datapro_token')
    if (!token) { navigate('/'); return }

    try {
      const res = await fetch('${import.meta.env.VITE_AUTH_URL}/api/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ planKey })
      })
      const data = await res.json()
      if (!res.ok) { setError(data.error || 'Failed to create subscription'); setLoading(null); return }

      const { subscriptionId, key } = data

      const rzp = new (window as any).Razorpay({
        key,
        subscription_id: subscriptionId,
        name: "The God's Tool",
        description: PLANS.find(p => p.key === planKey)?.name + ' Plan',
        handler: async (response: any) => {
          const verifyRes = await fetch('${import.meta.env.VITE_AUTH_URL}/api/verify-payment', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
            body: JSON.stringify({ ...response, planKey })
          })
          const verifyData = await verifyRes.json()
          if (verifyData.success) {
            localStorage.setItem('datapro_plan', planKey)
            navigate('/')
            window.location.reload()
          } else {
            setError('Payment verified failed. Contact support.')
          }
          setLoading(null)
        },
        modal: { ondismiss: () => setLoading(null) },
        prefill: { email: localStorage.getItem('datapro_email') || '' },
        theme: { color: '#7c3aed' }
      })
      rzp.open()
    } catch (err) {
      setError('Cannot connect to server.')
      setLoading(null)
    }
  }

  return (
    <div style={{ minHeight: '100vh', background: '#07050f', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '40px 16px' }}>
      <script src="https://checkout.razorpay.com/v1/checkout.js" />

      <div style={{ textAlign: 'center', marginBottom: '48px' }}>
        <div style={{ fontSize: '2.8rem', fontWeight: 900, fontFamily: 'Georgia, serif', background: 'linear-gradient(135deg, #e9d5ff, #a78bfa, #7c3aed)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent', backgroundClip: 'text' }}>
          Unlock The God's Tool
        </div>
        <p style={{ color: 'rgba(255,255,255,0.4)', marginTop: '12px', fontSize: '1rem', fontFamily: 'Georgia, serif' }}>
          Choose a plan to access AI-powered data cleaning
        </p>
      </div>

      {error && (
        <div style={{ marginBottom: '24px', background: 'rgba(220,38,38,0.12)', border: '1px solid rgba(220,38,38,0.3)', borderRadius: '10px', padding: '12px 20px', color: '#fca5a5', fontSize: '0.875rem', fontFamily: 'monospace' }}>
          ⚠ {error}
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(230px, 1fr))', gap: '20px', maxWidth: '1000px', width: '100%' }}>
        {PLANS.map(plan => (
          <div key={plan.key} style={{
            background: plan.highlight ? 'rgba(124,58,237,0.12)' : plan.bg,
            border: `1px solid ${plan.border}`,
            borderRadius: '16px',
            padding: '28px 24px',
            display: 'flex',
            flexDirection: 'column',
            gap: '16px',
            position: 'relative',
            boxShadow: plan.highlight ? '0 0 40px rgba(124,58,237,0.2)' : 'none'
          }}>
            {plan.badge && (
              <div style={{ position: 'absolute', top: '-13px', left: '50%', transform: 'translateX(-50%)', background: '#22d3ee', color: '#0a0a1a', fontSize: '0.7rem', fontWeight: 700, padding: '3px 12px', borderRadius: '20px', fontFamily: 'monospace', letterSpacing: '0.08em', whiteSpace: 'nowrap' }}>
                {plan.badge}
              </div>
            )}

            <div>
              <div style={{ color: plan.color, fontWeight: 700, fontSize: '0.85rem', fontFamily: 'monospace', letterSpacing: '0.1em', textTransform: 'uppercase' }}>{plan.name}</div>
              <div style={{ color: 'white', fontWeight: 900, fontSize: '2rem', fontFamily: 'Georgia, serif', lineHeight: 1.1, marginTop: '6px' }}>
                {plan.price}
                <span style={{ color: 'rgba(255,255,255,0.35)', fontWeight: 400, fontSize: '0.9rem' }}>{plan.period}</span>
              </div>
            </div>

            <div style={{ background: plan.ai ? 'rgba(52,211,153,0.08)' : 'rgba(239,68,68,0.08)', border: `1px solid ${plan.ai ? 'rgba(52,211,153,0.2)' : 'rgba(239,68,68,0.2)'}`, borderRadius: '8px', padding: '8px 12px', fontSize: '0.78rem', color: plan.ai ? '#34d399' : '#f87171', fontFamily: 'monospace' }}>
              {plan.aiLabel}
            </div>

            <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: '8px', flex: 1 }}>
              {plan.features.map(f => (
                <li key={f} style={{ display: 'flex', alignItems: 'flex-start', gap: '8px', fontSize: '0.82rem', color: 'rgba(255,255,255,0.65)', fontFamily: 'monospace' }}>
                  <span style={{ color: plan.color, marginTop: '1px' }}>✓</span>
                  {f}
                </li>
              ))}
            </ul>

            <button
              onClick={() => handleSubscribe(plan.key)}
              disabled={loading === plan.key}
              style={{
                width: '100%',
                padding: '13px',
                background: plan.highlight ? 'linear-gradient(135deg, #7c3aed, #4f46e5)' : `${plan.color}22`,
                border: `1px solid ${plan.color}55`,
                borderRadius: '10px',
                color: plan.highlight ? 'white' : plan.color,
                fontWeight: 700,
                fontSize: '0.875rem',
                cursor: loading === plan.key ? 'not-allowed' : 'pointer',
                fontFamily: 'monospace',
                letterSpacing: '0.05em',
                opacity: loading === plan.key ? 0.6 : 1,
                transition: 'opacity 0.2s'
              }}
            >
              {loading === plan.key ? 'Opening...' : 'Subscribe'}
            </button>
          </div>
        ))}
      </div>

      <button
        onClick={() => navigate('/')}
        style={{ marginTop: '32px', background: 'transparent', border: 'none', color: 'rgba(255,255,255,0.25)', cursor: 'pointer', fontSize: '0.8rem', fontFamily: 'monospace' }}
      >
        ← Back to app
      </button>
    </div>
  )
}