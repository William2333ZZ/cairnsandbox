import { describe, expect, it } from 'vitest'
import { rate } from '@/components/charts'

describe('rate', () => {
  it('turns a cumulative counter into a per-second rate', () => {
    expect(rate([{ t: 0, v: 0 }, { t: 5, v: 500 }, { t: 15, v: 2500 }])).toEqual([{ t: 5, v: 100 }, { t: 15, v: 200 }])
  })
  it('leaves a gap where the counter went backwards or a value is missing', () => {
    expect(rate([{ t: 0, v: 900 }, { t: 5, v: 10 }, { t: 10 }, { t: 15, v: 20 }]).map(p => p.v)).toEqual([null, null, null])
  })
})
