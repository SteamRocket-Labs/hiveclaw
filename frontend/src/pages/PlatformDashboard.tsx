import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { adminApi } from '../api/domains/admin';
import AdminRuntimeReconciliationSection from './admin-companies/AdminRuntimeReconciliationSection';
import './PlatformDashboard.css';

function formatTokens(n: number | null | undefined): string {
    if (n == null) return '-';
    if (n < 1000) return String(n);
    if (n < 1_000_000) return (n / 1000).toFixed(n < 10_000 ? 1 : 0) + 'K';
    if (n < 1_000_000_000) return (n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0) + 'M';
    return (n / 1_000_000_000).toFixed(1) + 'B';
}

export default function PlatformDashboard() {
    const { t } = useTranslation();
    const [timeRange, setTimeRange] = useState<30 | 7>(30);
    const [loadingStats, setLoadingStats] = useState(false);
    const [loadingLeaders, setLoadingLeaders] = useState(false);

    const [timeSeriesData, setTimeSeriesData] = useState<any[]>([]);
    const [topCompanies, setTopCompanies] = useState<any[]>([]);
    const [topAgents, setTopAgents] = useState<any[]>([]);

    const fetchTimeSeries = async (days: number) => {
        setLoadingStats(true);
        try {
            const end = new Date();
            const start = new Date();
            start.setDate(start.getDate() - days);
            const data = await adminApi.getMetricsTimeseries({ startDate: start.toISOString(), endDate: end.toISOString() }) as any[];
            setTimeSeriesData(data);
        } catch {
            // Endpoint may not exist yet — graceful degrade
        }
        setLoadingStats(false);
    };

    const fetchLeaderboards = async () => {
        setLoadingLeaders(true);
        try {
            const data = await adminApi.getMetricsLeaderboards() as any;
            setTopCompanies(data.top_companies || []);
            setTopAgents(data.top_agents || []);
        } catch {
            // Endpoint may not exist yet — graceful degrade
        }
        setLoadingLeaders(false);
    };

    useEffect(() => {
        fetchTimeSeries(timeRange);
    }, [timeRange]);

    useEffect(() => {
        fetchLeaderboards();
    }, []);

    const CustomTooltip = ({ active, payload, label }: any) => {
        if (active && payload && payload.length) {
            return (
                <div className="platform-dash-tooltip">
                    <div className="platform-dash-tooltip-label">{label}</div>
                    {payload.map((p: any, i: number) => (
                        <div key={i} className="platform-dash-tooltip-row">
                            <div className="platform-dash-tooltip-dot" style={{ background: p.stroke }} />
                            <span className="platform-dash-tooltip-name">{p.name}:</span>
                            <span className="platform-dash-tooltip-val">{p.dataKey.includes('tokens') ? formatTokens(p.value) : p.value}</span>
                        </div>
                    ))}
                </div>
            );
        }
        return null;
    };

    const ChartCard = ({ title, dataKeyTotal, dataKeyNew, color }: { title: string, dataKeyTotal: string, dataKeyNew: string, color: string }) => (
        <div className="card platform-dash-chart-card">
            <div className="platform-dash-card-title">{title}</div>
            <div className="platform-dash-chart-wrap">
                {loadingStats ? (
                    <div className="platform-dash-chart-loading">Loading...</div>
                ) : (
                    <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={timeSeriesData} margin={{ top: 5, right: 5, left: -20, bottom: 5 }}>
                            <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="var(--border-subtle)" />
                            <XAxis dataKey="date" tick={{ fontSize: 10, fill: 'var(--text-tertiary)' }} tickLine={false} axisLine={false} tickFormatter={(val) => val.substring(5)} />
                            <YAxis yAxisId="left" tick={{ fontSize: 10, fill: 'var(--text-tertiary)' }} tickLine={false} axisLine={false} tickFormatter={formatTokens} />
                            <Tooltip content={<CustomTooltip />} />
                            <Line yAxisId="left" type="monotone" dataKey={dataKeyTotal} name={`Cumulative`} stroke={color} strokeWidth={2} dot={false} activeDot={{ r: 4 }} />
                            <Line yAxisId="left" type="monotone" dataKey={dataKeyNew} name={`New`} stroke={color} opacity={0.3} strokeWidth={2} dot={false} strokeDasharray="4 4" />
                        </LineChart>
                    </ResponsiveContainer>
                )}
            </div>
        </div>
    );

    return (
        <div className="platform-dash-root">
            {/* Range Toggle */}
            <div className="platform-dash-range-bar">
                <div className="ui-segmented">
                    <button
                        onClick={() => setTimeRange(7)}
                        className={`ui-segmented-item ${timeRange === 7 ? 'active' : ''}`}>
                        Last 7 Days
                    </button>
                    <button
                        onClick={() => setTimeRange(30)}
                        className={`ui-segmented-item ${timeRange === 30 ? 'active' : ''}`}>
                        Last 30 Days
                    </button>
                </div>
            </div>

            {/* Charts Row */}
            <div className="platform-dash-charts-row">
                <ChartCard title="Companies" dataKeyTotal="total_companies" dataKeyNew="new_companies" color="#3b82f6" />
                <ChartCard title="Users" dataKeyTotal="total_users" dataKeyNew="new_users" color="#10b981" />
                <ChartCard title="Token Usage" dataKeyTotal="total_tokens" dataKeyNew="new_tokens" color="#8b5cf6" />
            </div>

            {/* Leaderboards */}
            <div className="platform-dash-charts-row">
                <div className="card platform-dash-board">
                    <div className="platform-dash-board-title">
                        Top 20 Companies by Tokens
                    </div>
                    {loadingLeaders ? (
                        <div className="platform-dash-board-loading">Loading...</div>
                    ) : (
                        <div>
                            {topCompanies.map((c, i) => (
                                <div key={i} className="platform-dash-board-row">
                                    <div className="platform-dash-board-rank-wrap">
                                        <span className="platform-dash-rank">#{i + 1}</span>
                                        <span className="platform-dash-board-name">{c.name}</span>
                                    </div>
                                    <div className="platform-dash-board-tokens">
                                        {formatTokens(c.tokens)}
                                    </div>
                                </div>
                            ))}
                            {topCompanies.length === 0 && <div className="platform-dash-board-empty">No data</div>}
                        </div>
                    )}
                </div>

                <div className="card platform-dash-board">
                    <div className="platform-dash-board-title">
                        Top 20 Agents by Tokens
                    </div>
                    {loadingLeaders ? (
                        <div className="platform-dash-board-loading">Loading...</div>
                    ) : (
                        <div>
                            {topAgents.map((a, i) => (
                                <div key={i} className="platform-dash-board-row">
                                    <div className="platform-dash-board-rank-wrap">
                                        <span className="platform-dash-rank">#{i + 1}</span>
                                        <div>
                                            <div className="platform-dash-board-name">{a.name}</div>
                                            <div className="platform-dash-board-sub">{a.company}</div>
                                        </div>
                                    </div>
                                    <div className="platform-dash-board-tokens">
                                        {formatTokens(a.tokens)}
                                    </div>
                                </div>
                            ))}
                            {topAgents.length === 0 && <div className="platform-dash-board-empty">No data</div>}
                        </div>
                    )}
                </div>
            </div>

            <AdminRuntimeReconciliationSection />
        </div>
    );
}
