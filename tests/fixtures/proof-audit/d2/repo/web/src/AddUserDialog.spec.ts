import { expect, test } from '@playwright/test';

test('an unknown address offers invite or create', async ({ page }) => {
  await page.goto('/accounts/7');
  await page.getByRole('button', { name: 'Add user' }).click();
  await page.getByLabel('Email').fill('new.person@example.com');
  await page.getByRole('button', { name: 'Continue' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('No account uses new.person@example.com');
  await expect(dialog.getByRole('button', { name: 'Send invite' }).first()).toHaveAttribute('aria-pressed', 'true');
  await expect(dialog.getByRole('button', { name: 'Create account now' })).toBeVisible();
  await expect(dialog).toContainText('They get an invite email');
  await dialog.getByRole('button', { name: 'Send invite' }).last().click();
  await expect(page.getByText('Invited new.person@example.com')).toBeVisible();
});

test('create account now explains that the account is made now', async ({ page }) => {
  await page.goto('/accounts/7');
  await page.getByRole('button', { name: 'Add user' }).click();
  await page.getByLabel('Email').fill('made.now@example.com');
  await page.getByRole('button', { name: 'Continue' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: 'Create account now' }).click();
  await expect(dialog.getByRole('button', { name: 'Create account now' })).toHaveAttribute('aria-pressed', 'true');
  await expect(dialog).toContainText('The account is created now');
  await dialog.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByText('Created an account for made.now@example.com')).toBeVisible();
});
