// @vitest-environment jsdom
import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { useState } from 'react';
import { AgentDetailNavigation } from './AgentDetailHeader';
import { AGENT_DETAIL_TABS } from './agentDetailPolicy';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => translateFromCatalog(zh, key) }) }));

it('reaches existing content through three sections with disclosures instead of thirteen tabs', () => {
  function Example() {
    const [active, setActive] = useState('status');
    return <AgentDetailNavigation visibleTabs={AGENT_DETAIL_TABS.filter(tab => tab !== 'a2a')} activeTab={active} onSelectTab={setActive}>
      <p>Content for {active}</p>
    </AgentDetailNavigation>;
  }
  const { container } = render(<Example />);
  expect(screen.getAllByRole('tab').map(tab => tab.textContent)).toEqual(['它能用什么', '它做过什么', '规则']);
  fireEvent.click(screen.getByRole('tab', { name: '它做过什么' }));
  expect(screen.getByText('Content for tasks')).toBeTruthy();
  fireEvent.click(screen.getByText('定时任务'));
  expect(screen.getByText('Content for aware')).toBeTruthy();
  fireEvent.click(screen.getByRole('tab', { name: '规则' }));
  expect(screen.getByText('Content for knowledge')).toBeTruthy();
  expect(container.querySelectorAll('.agent-workbench-subtab')).toHaveLength(0);
});
