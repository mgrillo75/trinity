import { test, expect } from '@playwright/test'

test.describe('smoke', () => {
  test('dashboard renders for authenticated admin', async ({ page }) => {
    await page.goto('/')
    // Top nav has Dashboard, Agents, Templates, Health, Ops, Keys, Settings.
    await expect(page.getByText(/^Dashboard$/i).first()).toBeVisible()
    await expect(page.getByText(/^Agents$/i).first()).toBeVisible()
    await expect(page.getByText(/^Settings$/i).first()).toBeVisible()
  })

  test('agents page loads', async ({ page }) => {
    await page.goto('/agents')
    await expect(page.getByText(/agent|create/i).first()).toBeVisible({ timeout: 10000 })
  })

  test('operating room page loads', async ({ page }) => {
    await page.goto('/operating-room')
    // Either a queue list, filters, an empty state, or the title.
    await expect(
      page.getByText(/operating|queue|priority|all types|no items/i).first()
    ).toBeVisible({ timeout: 10000 })
  })

  test('templates page loads', async ({ page }) => {
    await page.goto('/templates')
    await expect(page.getByText(/template/i).first()).toBeVisible({ timeout: 10000 })
  })
})
