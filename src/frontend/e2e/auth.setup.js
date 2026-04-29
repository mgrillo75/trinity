import { test as setup, expect } from '@playwright/test'

const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD
if (!ADMIN_PASSWORD) {
  throw new Error('ADMIN_PASSWORD env var must be set for e2e tests')
}

setup('authenticate as admin', async ({ page }) => {
  await page.goto('/')
  // Email auth is the default landing form; the password field only appears
  // after clicking the Admin Login fallback. Click it if visible (skipped
  // when admin-only mode is configured and the password field is shown
  // immediately).
  const passwordVisible = await page.locator('#password').isVisible().catch(() => false)
  if (!passwordVisible) {
    await page.getByText('Admin Login', { exact: false }).click()
  }
  await page.locator('#password').fill(ADMIN_PASSWORD)
  await page.getByRole('button', { name: /sign in as admin/i }).click()
  // Login successful when the URL is no longer /login.
  await expect(page).not.toHaveURL(/\/login/, { timeout: 10000 })
  // And the password field is gone.
  await expect(page.locator('#password')).not.toBeVisible({ timeout: 5000 })
  await page.context().storageState({ path: 'e2e/.auth/admin.json' })
})
