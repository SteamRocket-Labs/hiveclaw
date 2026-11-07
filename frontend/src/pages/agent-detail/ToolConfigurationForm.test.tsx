// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ToolConfigurationForm from './ToolConfigurationForm';
import type { AgentTool } from '../../api/domains/tools';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));

const masked = '__HIVE_SECRET_SET__';
afterEach(cleanup);
const tool = {
  agent_config: { headers: masked, vendor_pin: masked }, global_config: {},
  config_schema: { fields: [{ key: 'headers', label: 'Headers', type: 'object' }] },
} as unknown as AgentTool;

describe('masked settings', () => {
  it('retains a whole masked container and unknown fields when saving an old form', async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    render(<ToolConfigurationForm tool={tool} onSave={save} />);
    expect((screen.getByLabelText('Headers') as HTMLTextAreaElement).value).toBe('');
    expect(screen.getByLabelText('Headers').getAttribute('placeholder')).toBe('••••••');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({ headers: masked, vendor_pin: masked }));
  });

  it('lets the member replace a masked container with valid JSON', async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    render(<ToolConfigurationForm tool={tool} onSave={save} />);
    fireEvent.change(screen.getByLabelText('Headers'), { target: { value: '{"Authorization":"new-person-input"}' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({ headers: { Authorization: 'new-person-input' }, vendor_pin: masked }));
  });

  it('keeps masked JSON settings editable without a schema', async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    render(<ToolConfigurationForm tool={{ ...tool, config_schema: {} }} onSave={save} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: JSON.stringify({ headers: masked, vendor_pin: 'new-person-input' }) } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({ headers: masked, vendor_pin: 'new-person-input' }));
  });
});
