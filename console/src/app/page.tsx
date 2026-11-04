'use client'
import { useRouter } from 'next/navigation'
import { useEffect } from 'react'
export default function Home() { const r = useRouter(); useEffect(() => { r.replace('/overview/') }, [r]); return null }
