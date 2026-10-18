import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { systemApi } from '../../api/domains/system';
import EmployeeReadinessControls from '../../components/EmployeeReadinessControls';
import { isAdministratorRole } from '../../roles';
import { useAuthStore } from '../../stores';

function selectedCompany() {
  try { return localStorage.getItem('current_tenant_id') || ''; } catch { return ''; }
}

export default function CompanyOpeningGuide() {
  const user = useAuthStore((state) => state.user);
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const changed = (event: StorageEvent) => { if (event.key === 'current_tenant_id' || event.key === null) setGeneration((value) => value + 1); };
    window.addEventListener('storage', changed);
    return () => window.removeEventListener('storage', changed);
  }, []);
  const tenantId = selectedCompany() || user?.tenant_id;
  if (!user || !tenantId || !isAdministratorRole(user.role)) return null;
  return <CompanyOpening key={`${tenantId}:${user.id}:${generation}`} tenantId={tenantId} userId={user.id} />;
}

function CompanyOpening({ tenantId, userId }: { tenantId: string; userId: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState('');
  const scopeKey = `${tenantId}:${userId}`;
  const query = useQuery({ queryKey: ['company-opening', tenantId, userId],
    queryFn: () => systemApi.getOnboardingStatus(tenantId), enabled: open });
  const data = query.data;
  const confirmed = data?.tenant_id === tenantId && data.authenticated_user_id === userId && data.configuration_only === true;
  const failed = query.isError || (data !== undefined && !confirmed);
  const employees = confirmed ? data.employees : [];
  const employeeId = selected || (employees.length === 1 ? employees[0].id : '');
  const employee = employees.find((item) => item.id === employeeId);
  const labels = {
    llm: t('companyOpening.models', 'Models for native employees'),
    agent: t('companyOpening.employee', 'Create a digital employee'),
    channel: t('companyOpening.channel', 'Employee channel configuration'),
    org: t('companyOpening.directory', 'Company directory'),
    sso: t('companyOpening.sso', 'OIDC sign-in configuration'),
  };
  function item(key: 'llm' | 'agent' | 'channel' | 'org' | 'sso') {
    const value = data?.items.find((row) => row.key === key);
    return <li key={key}>
      {value?.link ? <Link to={value.link}>{labels[key]}</Link> : <span>{labels[key]}</span>}
      {' · '}{value?.completed ? t('companyOpening.configured', 'Configured') : t('companyOpening.missing', 'Not configured')}
    </li>;
  }
  return <details className="workbench-panel" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t('companyOpening.title', 'Get started with your company')}</summary>
    {open && <div>
      {failed ? <p role="alert">{t('companyOpening.unavailable', 'Could not confirm this company’s setup. Refresh to retry; previous company details are hidden.')}</p>
        : !confirmed ? <p role="status">{t('companyOpening.loading', 'Reading the current company setup…')}</p> : <>
          <h2>{data.tenant_name}</h2>
          <p>{t('companyOpening.signedIn', 'You are signed in to this company. Configuration below does not prove that a provider, worker or connected application can complete a task.')}</p>
          <ol>{item('llm')}{item('agent')}</ol>
          <p>{t('companyOpening.runtimeChoice', 'Native employees use an approved company model. Connected employees use their own runtime. Check the employee you intend to work with; a computer, SSO, channel or directory sync is not required for every task.')}</p>
          {data.employees.length > 0 && <>
            <label htmlFor="company-opening-employee">{t('companyOpening.selectEmployee', 'Employee to check')}</label>
            <select id="company-opening-employee" className="form-input" value={employeeId} onChange={(event) => setSelected(event.target.value)}>
              <option value="">{t('companyOpening.chooseEmployee', 'Choose an employee')}</option>
              {data.employees.map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}
            </select>
            {employee && <>
              <EmployeeReadinessControls key={`${scopeKey}:${employee.id}`} agentId={employee.id} scopeKey={scopeKey} />
              <Link className="btn btn-secondary" to={`/agents/${employee.id}`}>{t('companyOpening.openEmployee', 'Open employee work')}</Link>
            </>}
          </>}
          <details>
            <summary>{t('companyOpening.optional', 'Optional company integrations')}</summary>
            <p>{t('companyOpening.optionalBoundary', 'These are saved configuration records. Sign-in, channel delivery and directory sync require their own real checks.')}</p>
            <ul>{item('channel')}{item('org')}{item('sso')}</ul>
          </details>
        </>}
      <button type="button" className="btn btn-ghost" disabled={query.isFetching} onClick={() => { void query.refetch(); }}>{t('common.refresh', 'Refresh')}</button>
    </div>}
  </details>;
}
