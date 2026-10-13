import { expect, it } from 'vitest'

import config from '../../vite.config'

it('proxies API requests without rewriting the browser origin used by auth checks', () => {
  expect(config.server?.proxy?.['/api']).toEqual({
    target: 'http://127.0.0.1:8000',
    changeOrigin: false,
  })
})

it('keeps Playwright end-to-end specs out of the Vitest unit suite', () => {
  expect(config.test?.exclude).toContain('e2e/**')
})
