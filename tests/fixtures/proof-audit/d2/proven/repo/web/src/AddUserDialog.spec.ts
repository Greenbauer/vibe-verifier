import { expect, test } from '@playwright/test';

test('an unknown address offers invite or create', async ({ page }) => {
  await page.goto('/accounts/7');
  await page.getByRole('button', { name: 'Add user' }).click();
  await page.getByLabel('Email').fill('new.person@example.com');
  await page.getByRole('button', { name: 'Continue' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('No account uses new.person@example.com');
  // The choice is a radio, and the one button that sends names what it will do.
  await expect(dialog.getByRole('radio', { name: 'Email them an invite' })).toBeChecked();
  await expect(dialog.getByRole('button')).toHaveCount(1);
  await expect(dialog.getByRole('button', { name: 'Send invite' })).toBeVisible();
  await expect(dialog).toContainText('They get an invite email');
  await dialog.getByRole('radio', { name: 'Make the account now' }).check();
  await expect(dialog.getByRole('radio', { name: 'Make the account now' })).toBeChecked();
  await expect(dialog.getByRole('button')).toHaveCount(1);
  await expect(dialog.getByRole('button', { name: 'Create account' })).toBeVisible();
  await expect(dialog).toContainText('The account is created now');
  await dialog.getByRole('radio', { name: 'Email them an invite' }).check();
  await dialog.getByRole('button', { name: 'Send invite' }).click();
  await expect(page.getByText('Invited new.person@example.com')).toBeVisible();
});
