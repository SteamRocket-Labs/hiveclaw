import { useEffect, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
    channelApi,
    type AgentChannelConfig,
    type FeishuPlatformRegion,
} from '../api/domains/channels';
import { toolsApi, type FeishuRuntimeStatus } from '../api/domains/tools';
import FeishuAppRegistrationSetup from './FeishuAppRegistrationSetup';
import FeishuRuntimeStatusCard from './FeishuRuntimeStatusCard';
import WeChatPersonalSetup from './WeChatPersonalSetup';
import './ChannelConfig.css';

// ─── Types ──────────────────────────────────────────────
interface ChannelConfigProps {
    mode: 'create' | 'edit';
    agentId?: string;          // required for edit mode
    canManage?: boolean;       // edit mode: whether current user can manage
    values?: Record<string, string>;
    onChange?: (values: Record<string, string>) => void;
}

interface ChannelField {
    key: string;
    label: string;
    placeholder?: string;
    type?: 'text' | 'password';
    required?: boolean;
}

interface GuideConfig {
    prefix: string;           // i18n key prefix e.g. 'channelGuide.slack'
    steps: number;
    noteKey?: string;         // override note key
}

interface ChannelDef {
    id: string;
    icon: ReactNode;
    nameKey: string;
    nameFallback: string;
    desc: string;
    // API endpoint slug: e.g. 'slack-channel', 'discord-channel'
    apiSlug?: string;
    // Feishu uses channelApi instead of fetchAuth
    useChannelApi?: boolean;
    // Fields for configuration form
    fields: ChannelField[];
    // Setup guide
    guide: GuideConfig;
    // Whether this channel supports connection_mode toggle (feishu, wecom)
    connectionMode?: boolean;
    // WebSocket guide config (when connection_mode === 'websocket')
    wsGuide?: GuideConfig;
    // Whether this channel shows feishu permission JSON block
    showPermJson?: boolean;
    // Webhook URL label
    webhookLabel?: string;
    // Channels only shown in edit mode (not in create wizard)
    editOnly?: boolean;
    // Custom fields for websocket mode (wecom)
    wsFields?: ChannelField[];
    // Optional connection test action for API-backed channels.
    hasTestConnection?: boolean;
    // QR scan mode (no form fields, renders a QR scan component instead)
    qrScanMode?: boolean;
}

const DEFAULT_FEISHU_PLATFORM_REGION: FeishuPlatformRegion = 'feishu_cn';
const FEISHU_PLATFORM_OPTIONS: Array<{ value: FeishuPlatformRegion; labelKey: string; fallback: string }> = [
    { value: 'feishu_cn', labelKey: 'agent.settings.channel.platformFeishuCn', fallback: 'Feishu (China)' },
    { value: 'lark_global', labelKey: 'agent.settings.channel.platformLarkGlobal', fallback: 'Lark (Global)' },
];

function normalizeFeishuPlatformRegion(value?: string): FeishuPlatformRegion {
    return value === 'lark_global' ? 'lark_global' : DEFAULT_FEISHU_PLATFORM_REGION;
}

function getFeishuPlatformOption(region: FeishuPlatformRegion) {
    return FEISHU_PLATFORM_OPTIONS.find(option => option.value === region) || FEISHU_PLATFORM_OPTIONS[0];
}

// ─── SVG Icons ──────────────────────────────────────────
const SlackIcon = <img src="/slack.png" alt="Slack" width="20" height="20" className="channel-config-icon" />;

const DiscordIcon = <img src="/discord.png" alt="Discord" width="20" height="20" className="channel-config-icon" />;

const FeishuIcon = <img src="/feishu.png" alt="Feishu" width="20" height="20" className="channel-config-icon" />;

const TeamsIcon = <img src="/teams.png" alt="Teams" width="20" height="20" className="channel-config-icon" />;

const WeComIcon = <img src="/wecom.png" alt="WeCom" width="20" height="20" className="channel-config-icon" />;

const DingTalkIcon = <img src="/dingtalk.png" alt="DingTalk" width="20" height="20" className="channel-config-icon" />;

const WeChatIcon = <svg width="20" height="20" viewBox="0 0 24 24" fill="#07C160"><path d="M9.5 4C5.36 4 2 6.69 2 10c0 1.89 1.08 3.56 2.78 4.66l-.7 2.1 2.45-1.23c.78.22 1.6.35 2.47.37-.17-.53-.25-1.1-.25-1.68 0-3.45 3.36-6.24 7.5-6.24.26 0 .51.01.76.04C16.13 5.64 13.1 4 9.5 4zm-3 4.5a1 1 0 110-2 1 1 0 010 2zm5 0a1 1 0 110-2 1 1 0 010 2zM22 14.22c0-2.8-2.9-5.06-6.5-5.06S9 11.42 9 14.22c0 2.8 2.9 5.06 6.5 5.06.7 0 1.38-.1 2.02-.27l2 1-.57-1.7C20.98 17.33 22 15.88 22 14.22zm-8.5-1a.88.88 0 110-1.75.88.88 0 010 1.75zm4 0a.88.88 0 110-1.75.88.88 0 010 1.75z"/></svg>;
const AgentBayIcon = <span className="channel-config-emoji-sm">🌩️</span>;

// Eye icons for password toggle
const EyeOpen = <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" /></svg>;
const EyeClosed = <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94" /><path d="M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19" /><line x1="1" y1="1" x2="23" y2="23" /></svg>;

// ─── Channel Registry ───────────────────────────────────
const CHANNEL_REGISTRY: ChannelDef[] = [
    {
        id: 'slack',
        icon: SlackIcon,
        nameKey: 'common.channels.slack',
        nameFallback: 'Slack',
        desc: 'Slack Bot',
        apiSlug: 'slack-channel',
        fields: [
            { key: 'bot_token', label: 'Bot Token', placeholder: 'xoxb-...', type: 'password', required: true },
            { key: 'signing_secret', label: 'Signing Secret', type: 'password', required: true },
        ],
        guide: { prefix: 'channelGuide.slack', steps: 8 },
        webhookLabel: 'Webhook URL (Event Subscriptions URL)',
    },
    {
        id: 'discord',
        icon: DiscordIcon,
        nameKey: 'common.channels.discord',
        nameFallback: 'Discord',
        desc: 'Gateway / Webhook',
        apiSlug: 'discord-channel',
        connectionMode: true,
        fields: [
            { key: 'application_id', label: 'Application ID', placeholder: '1234567890', required: true },
            { key: 'bot_token', label: 'Bot Token', type: 'password', required: true },
            { key: 'public_key', label: 'Public Key', required: true },
        ],
        wsFields: [
            { key: 'bot_token', label: 'Bot Token', type: 'password', required: true },
        ],
        guide: { prefix: 'channelGuide.discord', steps: 7 },
        wsGuide: { prefix: 'channelGuide.discord', steps: 4 },
        webhookLabel: 'Interactions Endpoint URL',
    },
    {
        id: 'teams',
        icon: TeamsIcon,
        nameKey: 'common.channels.teams',
        nameFallback: 'Microsoft Teams',
        desc: 'Teams Bot',
        apiSlug: 'teams-channel',
        fields: [
            { key: 'app_id', label: 'App ID (Client ID)', placeholder: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx', required: true },
            { key: 'app_secret', label: 'App Secret (Client Secret)', type: 'password', required: true },
            { key: 'tenant_id', label: 'channelGuide.teams.tenantId', placeholder: 'channelGuide.teams.tenantIdPlaceholder' },
        ],
        guide: { prefix: 'channelGuide.teams', steps: 5 },
        webhookLabel: 'Messaging Endpoint URL',
    },
    {
        id: 'feishu',
        icon: FeishuIcon,
        nameKey: 'agent.settings.channel.feishu',
        nameFallback: 'Feishu / Lark',
        desc: 'Feishu / Lark',
        useChannelApi: true,
        connectionMode: true,
        fields: [
            { key: 'app_id', label: 'App ID', placeholder: 'cli_xxxxxxxxxxxxxxxx', required: true },
            { key: 'app_secret', label: 'App Secret', type: 'password', required: true },
            { key: 'encrypt_key', label: 'Encrypt Key', type: 'password' },
        ],
        guide: { prefix: 'channelGuide.feishu', steps: 8 },
        wsGuide: { prefix: 'channelGuide.feishu', steps: 8 },
        showPermJson: true,
        webhookLabel: 'Webhook URL',
    },
    {
        id: 'wecom',
        icon: WeComIcon,
        nameKey: 'common.channels.wecom',
        nameFallback: 'WeCom',
        desc: 'WebSocket / Webhook',
        apiSlug: 'wecom-channel',
        connectionMode: true,
        fields: [
            { key: 'corp_id', label: 'CorpID', required: true },
            { key: 'wecom_agent_id', label: 'AgentID', required: true },
            { key: 'secret', label: 'Secret', type: 'password', required: true },
            { key: 'token', label: 'Token', required: true },
            { key: 'encoding_aes_key', label: 'EncodingAESKey', required: true },
        ],
        wsFields: [
            { key: 'bot_id', label: 'Bot ID', placeholder: 'aibXXXXXXXXXXXX', required: true },
            { key: 'bot_secret', label: 'Bot Secret', type: 'password', required: true },
        ],
        guide: { prefix: 'channelGuide.wecom', steps: 6 },
        wsGuide: { prefix: 'channelGuide.wecom', steps: 6 },
        webhookLabel: 'Webhook URL',
    },
    {
        id: 'dingtalk',
        icon: DingTalkIcon,
        nameKey: 'common.channels.dingtalk',
        nameFallback: 'DingTalk',
        desc: 'Stream Mode',
        apiSlug: 'dingtalk-channel',
        fields: [
            { key: 'app_key', label: 'AppKey', type: 'password', required: true },
            { key: 'app_secret', label: 'AppSecret', type: 'password', required: true },
        ],
        guide: { prefix: 'channelGuide.dingtalk', steps: 6 },
    },
    {
        id: 'agentbay',
        icon: AgentBayIcon,
        nameKey: 'common.channels.agentbay',
        nameFallback: 'AgentBay',
        desc: 'Browser & Code Execution (阿里云)',
        apiSlug: 'agentbay-channel',
        hasTestConnection: true,
        editOnly: true,
        fields: [
            { key: 'api_key', label: 'API Key', type: 'password', required: true },
            { key: 'base_url', label: 'Base URL', placeholder: 'https://agentbay.aliyuncs.com/api/v1' },
        ],
        guide: { prefix: 'channelGuide.agentbay', steps: 3 },
    },
    {
        id: 'email',
        icon: <span className="channel-config-emoji">📧</span>,
        nameKey: 'common.channels.email',
        nameFallback: 'Email',
        desc: 'SMTP / IMAP',
        apiSlug: 'email-channel',
        editOnly: true,
        fields: [
            { key: 'email_provider', label: 'Provider' },
            { key: 'email_address', label: 'Email Address' },
            { key: 'auth_code', label: 'Authorization Code / App Password', type: 'password' as const, required: true },
        ],
        guide: { prefix: 'channelGuide.email', steps: 4 },
    },
    {
        id: 'telegram',
        icon: <span className="channel-config-emoji">✈️</span>,
        nameKey: 'common.channels.telegram',
        nameFallback: 'Telegram',
        desc: 'Telegram Bot',
        apiSlug: 'telegram-channel',
        editOnly: true,
        fields: [
            { key: 'bot_token', label: 'Bot Token', type: 'password' as const, required: true },
        ],
        guide: { prefix: 'channelGuide.telegram', steps: 5 },
    },
    {
        id: 'wechat_personal',
        icon: WeChatIcon,
        nameKey: 'common.channels.wechatPersonal',
        nameFallback: 'WeChat',
        desc: 'Personal WeChat (iLink)',
        apiSlug: 'wechat-personal',
        editOnly: true,
        qrScanMode: true,
        fields: [],
        guide: { prefix: 'channelGuide.wechatPersonal', steps: 3 },
    },
];

// Channels hidden from UI (kept in registry for future use)
const HIDDEN_CHANNELS = new Set(['teams', 'agentbay']);
const VISIBLE_CHANNELS = CHANNEL_REGISTRY.filter(ch => !HIDDEN_CHANNELS.has(ch.id));

// ─── Feishu Permission JSON ─────────────────────────────
const FEISHU_BASIC_PERM_JSON = '{"scopes":{"tenant":["contact:contact.base:readonly","contact:user.base:readonly","contact:user.id:readonly","im:chat","im:message","im:message.group_at_msg:readonly","im:message.p2p_msg:readonly","im:message:send_as_bot","im:resource"],"user":[]}}';

const FEISHU_BASIC_PERM_DISPLAY = `{
  "scopes": {
    "tenant": [
      "contact:contact.base:readonly",
      "contact:user.base:readonly",
      "contact:user.id:readonly",
      "im:chat",
      "im:message",
      "im:message.group_at_msg:readonly",
      "im:message.p2p_msg:readonly",
      "im:message:send_as_bot",
      "im:resource"
    ],
    "user": []
  }
}`;

const FEISHU_FULL_PERM_JSON = '{"scopes":{"tenant":["approval:approval","bitable:app","bitable:record","bitable:table","calendar:calendar","calendar:event","contact:contact.base:readonly","contact:user.base:readonly","contact:user.employee_id:readonly","contact:user.id:readonly","docx:document","docs:document.content","drive:drive","im:chat","im:message","im:message.group_at_msg:readonly","im:message.p2p_msg:readonly","im:message:send_as_bot","im:resource","task:task"],"user":[]}}';

const FEISHU_FULL_PERM_DISPLAY = `{
  "scopes": {
    "tenant": [
      "approval:approval",
      "bitable:app",
      "bitable:record",
      "bitable:table",
      "calendar:calendar",
      "calendar:event",
      "contact:contact.base:readonly",
      "contact:user.base:readonly",
      "contact:user.employee_id:readonly",
      "contact:user.id:readonly",
      "docx:document",
      "docs:document.content",
      "drive:drive",
      "im:chat",
      "im:message",
      "im:message.group_at_msg:readonly",
      "im:message.p2p_msg:readonly",
      "im:message:send_as_bot",
      "im:resource",
      "task:task"
    ],
    "user": []
  }
}`;

// ─── Copy Button helper ─────────────────────────────────
function CopyBtn({ url }: { url: string }) {
    return (
        <button title="Copy" className="channel-config-copy-btn"
            onClick={() => navigator.clipboard.writeText(url)}>
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <rect x="4" y="4" width="9" height="11" rx="1.5" /><path d="M3 11H2a1 1 0 01-1-1V2a1 1 0 011-1h8a1 1 0 011 1v1" />
            </svg>
        </button>
    );
}

// ─── Main Component ─────────────────────────────────────
export default function ChannelConfig({ mode, agentId, canManage = true, values, onChange }: ChannelConfigProps) {
    const { t } = useTranslation();
    const queryClient = useQueryClient();

    // Collapsible state per channel
    const [openChannels, setOpenChannels] = useState<Record<string, boolean>>((): Record<string, boolean> => {
        if (mode === 'edit') {
            return { feishu: true };
        }
        return { feishu: true };
    });
    const toggleChannel = (id: string) => setOpenChannels(prev => ({ ...prev, [id]: !prev[id] }));

    // Editing state per channel (edit mode only)
    const [editingChannels, setEditingChannels] = useState<Record<string, boolean>>({});
    const setEditing = (id: string, val: boolean) => setEditingChannels(prev => ({ ...prev, [id]: val }));

    // Form state per channel (edit mode only)
    const [forms, setForms] = useState<Record<string, Record<string, string>>>({});
    const setFormField = (channelId: string, key: string, val: string) =>
        setForms(prev => ({ ...prev, [channelId]: { ...prev[channelId], [key]: val } }));
    const getForm = (channelId: string) => forms[channelId] || {};

    // Connection mode state for feishu/wecom (edit mode)
    const [connectionModes, setConnectionModes] = useState<Record<string, string>>({
        feishu: 'websocket',
        wecom: 'websocket',
        discord: 'gateway',
    });
    const [feishuPlatformRegions, setFeishuPlatformRegions] = useState<Record<string, FeishuPlatformRegion>>({
        feishu: DEFAULT_FEISHU_PLATFORM_REGION,
    });
    const [feishuQrOpen, setFeishuQrOpen] = useState(false);

    // Password visibility
    const [showPwds, setShowPwds] = useState<Record<string, boolean>>({});
    const togglePwd = (fieldId: string) => setShowPwds(p => ({ ...p, [fieldId]: !p[fieldId] }));
    const [feishuPermissionPreset, setFeishuPermissionPreset] = useState<'basic' | 'full'>('full');

    // AgentBay test connection state
    const [agentbayTesting, setAgentbayTesting] = useState(false);
    const [agentbayTestResult, setAgentbayTestResult] = useState<{ ok: boolean; message?: string; error?: string } | null>(null);


    // ─── Edit mode: queries for each channel ────────────
    const enabled = mode === 'edit' && !!agentId;

    const { data: feishuConfig } = useQuery({
        queryKey: ['channel', agentId],
        queryFn: () => channelApi.get(agentId!),
        enabled: enabled,
        refetchInterval: (query) => {
            if (feishuQrOpen) return 2000;
            const channel = query.state.data as AgentChannelConfig | null | undefined;
            return channel?.is_configured
                && channel?.extra_config?.connection_mode === 'websocket'
                && !channel?.is_connected
                ? 2000
                : false;
        },
    });
    const { data: feishuWebhook } = useQuery({
        queryKey: ['webhook-url', agentId],
        queryFn: () => channelApi.webhookUrl(agentId!),
        enabled: enabled,
    });
    const { data: slackConfig } = useQuery({
        queryKey: ['slack-channel', agentId],
        queryFn: () => channelApi.getChannelConfig(agentId!, 'slack-channel'),
        enabled: enabled,
    });
    const { data: slackWebhook } = useQuery({
        queryKey: ['slack-webhook-url', agentId],
        queryFn: () => channelApi.getChannelWebhook(agentId!, 'slack-channel'),
        enabled: enabled,
    });
    const { data: discordConfig } = useQuery({
        queryKey: ['discord-channel', agentId],
        queryFn: () => channelApi.getChannelConfig(agentId!, 'discord-channel'),
        enabled: enabled,
    });
    const { data: discordWebhook } = useQuery({
        queryKey: ['discord-webhook-url', agentId],
        queryFn: () => channelApi.getChannelWebhook(agentId!, 'discord-channel'),
        enabled: enabled,
    });
    const { data: teamsConfig } = useQuery({
        queryKey: ['teams-channel', agentId],
        queryFn: () => channelApi.getChannelConfig(agentId!, 'teams-channel'),
        enabled: enabled,
    });
    const { data: teamsWebhook } = useQuery({
        queryKey: ['teams-webhook-url', agentId],
        queryFn: () => channelApi.getChannelWebhook(agentId!, 'teams-channel'),
        enabled: enabled,
    });
    const { data: dingtalkConfig } = useQuery({
        queryKey: ['dingtalk-channel', agentId],
        queryFn: () => channelApi.getChannelConfig(agentId!, 'dingtalk-channel'),
        enabled: enabled,
    });
    const { data: wecomConfig } = useQuery({
        queryKey: ['wecom-channel', agentId],
        queryFn: () => channelApi.getChannelConfig(agentId!, 'wecom-channel'),
        enabled: enabled,
    });
    const { data: wecomWebhook } = useQuery({
        queryKey: ['wecom-webhook-url', agentId],
        queryFn: () => channelApi.getChannelWebhook(agentId!, 'wecom-channel'),
        enabled: enabled,
    });
    const { data: agentbayConfig } = useQuery({
        queryKey: ['agentbay-channel', agentId],
        queryFn: () => channelApi.getChannelConfig(agentId!, 'agentbay-channel'),
        enabled: enabled,
    });
    const { data: telegramConfig } = useQuery({
        queryKey: ['telegram-channel', agentId],
        queryFn: () => channelApi.getChannelConfig(agentId!, 'telegram-channel'),
        enabled: enabled,
    });
    const { data: telegramWebhook } = useQuery({
        queryKey: ['telegram-webhook-url', agentId],
        queryFn: () => channelApi.getChannelWebhook(agentId!, 'telegram-channel'),
        enabled: enabled,
    });
    const { data: wechatPersonalStatus } = useQuery({
        queryKey: ['wechat-personal-status', agentId],
        queryFn: () => channelApi.wechatPersonalStatus(agentId!),
        enabled: enabled,
    });
    const { data: feishuRuntimeStatus } = useQuery<FeishuRuntimeStatus | null>({
        queryKey: ['feishu-runtime-status', agentId],
        queryFn: () => toolsApi.getAgentFeishuRuntimeStatus(agentId!),
        enabled: enabled,
    });

    useEffect(() => {
        const configuredRegion = feishuConfig?.extra_config?.platform_region;
        if (!configuredRegion) return;
        setFeishuPlatformRegions(prev => ({
            ...prev,
            feishu: normalizeFeishuPlatformRegion(configuredRegion),
        }));
    }, [feishuConfig?.extra_config?.platform_region]);


    // Helper: get config data for a channel
    const getConfig = (id: string): any => {
        switch (id) {
            case 'feishu': return feishuConfig;
            case 'slack': return slackConfig;
            case 'discord': return discordConfig;
            case 'teams': return teamsConfig;
            case 'dingtalk': return dingtalkConfig;
            case 'wecom': return wecomConfig;
            case 'agentbay': return agentbayConfig;
            case 'telegram': return telegramConfig;
            case 'wechat_personal': return wechatPersonalStatus;
            default: return null;
        }
    };

    // Helper: get webhook data for a channel
    const getWebhook = (id: string): any => {
        switch (id) {
            case 'feishu': return feishuWebhook;
            case 'slack': return slackWebhook;
            case 'discord': return discordWebhook;
            case 'teams': return teamsWebhook;
            case 'wecom': return wecomWebhook;
            case 'telegram': return telegramWebhook;
            default: return null;
        }
    };

    // ─── Edit mode: mutations ───────────────────────────
    const saveMutation = useMutation({
        mutationFn: ({ ch, data }: { ch: ChannelDef; data: any }) => {
            if (ch.useChannelApi) {
                return channelApi.create(agentId!, data);
            }
            return channelApi.createChannelConfig(agentId!, ch.apiSlug!, data);
        },
        onSuccess: (_d, { ch }) => {
            const keys = ch.useChannelApi
                ? [['channel', agentId]]
                : [[`${ch.apiSlug}`, agentId], [`${ch.id}-webhook-url`, agentId]];
            keys.forEach(k => queryClient.invalidateQueries({ queryKey: k }));
            // Reset form
            setForms(prev => ({ ...prev, [ch.id]: {} }));
            setEditing(ch.id, false);
        },
    });

    const deleteMutation = useMutation({
        mutationFn: ({ ch }: { ch: ChannelDef }) => {
            if (ch.useChannelApi) {
                return channelApi.delete(agentId!);
            }
            return channelApi.deleteChannelConfig(agentId!, ch.apiSlug!);
        },
        onSuccess: (_d, { ch }) => {
            const keys = ch.useChannelApi
                ? [['channel', agentId]]
                : [[`${ch.apiSlug}`, agentId]];
            keys.forEach(k => queryClient.invalidateQueries({ queryKey: k }));
            if (ch.id === 'agentbay') setAgentbayTestResult(null);
        },
    });

    const testAgentBay = async () => {
        setAgentbayTesting(true);
        setAgentbayTestResult(null);
        try {
            const res = await channelApi.testChannelConfig(agentId!, 'agentbay-channel') as { ok: boolean; message?: string; error?: string };
            setAgentbayTestResult(res);
        } catch (e: any) {
            setAgentbayTestResult({ ok: false, error: String(e) });
        }
        setAgentbayTesting(false);
    };

    // ─── Build save payload for a channel ───────────────
    const buildPayload = (ch: ChannelDef, form: Record<string, string>) => {
        if (ch.id === 'feishu') {
            return {
                channel_type: 'feishu',
                app_id: form.app_id,
                app_secret: form.app_secret,
                encrypt_key: form.encrypt_key || undefined,
                extra_config: {
                    connection_mode: connectionModes.feishu || 'websocket',
                    platform_region: normalizeFeishuPlatformRegion(feishuPlatformRegions.feishu),
                },
            };
        }
        if (ch.id === 'wecom') {
            const connMode = connectionModes.wecom || 'websocket';
            if (connMode === 'websocket') {
                return { connection_mode: 'websocket', bot_id: form.bot_id, bot_secret: form.bot_secret };
            }
            return { ...form, connection_mode: 'webhook' };
        }
        if (ch.id === 'discord') {
            const connMode = connectionModes.discord || 'gateway';
            if (connMode === 'websocket') {
                return { bot_token: form.bot_token, connection_mode: 'gateway' };
            }
            return { ...form, connection_mode: 'webhook' };
        }
        // Generic channels
        return form;
    };

    // ─── Render guide steps ─────────────────────────────
    const renderGuide = (guide: GuideConfig, isWs: boolean, ch: ChannelDef) => {
        const prefix = isWs && ch.wsGuide ? `${ch.wsGuide.prefix}.ws_step` : `${guide.prefix}.step`;
        const stepCount = isWs && ch.wsGuide ? ch.wsGuide.steps : guide.steps;
        const noteKey = isWs && ch.wsGuide ? `${ch.wsGuide.prefix}.ws_note` : (guide.noteKey || `${guide.prefix}.note`);
        const permJson = feishuPermissionPreset === 'basic' ? FEISHU_BASIC_PERM_JSON : FEISHU_FULL_PERM_JSON;
        const permDisplay = feishuPermissionPreset === 'basic' ? FEISHU_BASIC_PERM_DISPLAY : FEISHU_FULL_PERM_DISPLAY;

        return (
            <details className="channel-config-guide">
                <summary className="channel-config-guide-summary">
                    <span className="channel-config-guide-marker">&#9654;</span> {t('channelGuide.setupGuide')}
                </summary>
                <ol className="channel-config-guide-steps">
                    {Array.from({ length: stepCount }, (_, i) => (
                        <li key={i}>{t(`${prefix}${i + 1}`)}</li>
                    ))}
                </ol>
                {ch.showPermJson && (
                    <div className="channel-config-perm">
                        <div className="channel-config-perm-header">
                            <div className="channel-config-perm-header-left">
                                <span className="channel-config-perm-label">{t('channelGuide.feishuPermJson')}</span>
                                <button
                                    type="button"
                                    className={`channel-config-perm-preset${feishuPermissionPreset === 'basic' ? ' is-active' : ''}`}
                                    onClick={() => setFeishuPermissionPreset('basic')}
                                >
                                    {t('channelGuide.feishuPermBasic', 'Basic Permissions')}
                                </button>
                                <button
                                    type="button"
                                    className={`channel-config-perm-preset${feishuPermissionPreset === 'full' ? ' is-active' : ''}`}
                                    onClick={() => setFeishuPermissionPreset('full')}
                                >
                                    {t('channelGuide.feishuPermFull', 'Full Permissions')}
                                </button>
                            </div>
                            <button type="button" className="channel-config-perm-copy"
                                onClick={(e) => {
                                    const btn = e.currentTarget;
                                    navigator.clipboard.writeText(permJson).then(() => {
                                        const o = btn.textContent;
                                        btn.textContent = t('channelGuide.feishuPermCopied');
                                        btn.style.color = 'rgb(16,185,129)';
                                        setTimeout(() => { btn.textContent = o; btn.style.color = ''; }, 1500);
                                    });
                                }}>{t('channelGuide.feishuPermCopy')}</button>
                        </div>
                        <pre className="channel-config-perm-pre">{permDisplay}</pre>
                    </div>
                )}
                <div className="channel-config-guide-note">
                    {t(noteKey)}
                </div>
            </details>
        );
    };

    // ─── Render a password field with toggle ─────────────
    const renderField = (field: ChannelField, channelId: string, fieldValue: string, onFieldChange: (val: string) => void) => {
        const fieldId = `${channelId}_${field.key}`;
        const isSecret = field.type === 'password';
        const labelText = field.label.startsWith('channelGuide.') ? t(field.label) : field.label;
        const placeholderText = field.placeholder?.startsWith('channelGuide.') ? t(field.placeholder) : field.placeholder;

        return (
            <div key={field.key}>
                <label className="channel-config-field-label">
                    {labelText} {field.required && '*'}
                    {!field.required && <span className="channel-config-field-optional"> (Optional)</span>}
                </label>
                <div className="channel-config-field-input-wrap">
                    <input
                        className={mode === 'edit'
                            ? `input channel-config-field-input${isSecret ? ' channel-config-field-input-secret' : ''}`
                            : 'form-input'}
                        type={isSecret && !showPwds[fieldId] ? 'password' : 'text'}
                        value={fieldValue}
                        onChange={e => onFieldChange(e.target.value)}
                        placeholder={placeholderText || ''}
                    />
                    {isSecret && (
                        <button type="button" onClick={() => togglePwd(fieldId)}
                            className="channel-config-pwd-toggle">
                            {showPwds[fieldId] ? EyeClosed : EyeOpen}
                        </button>
                    )}
                </div>
                {/* Tenant ID hint for Teams */}
                {channelId === 'teams' && field.key === 'tenant_id' && (
                    <div className="channel-config-field-hint">{t('channelGuide.teams.tenantIdHint')}</div>
                )}
            </div>
        );
    };

    const renderFeishuPlatformSelector = (
        channelId: string,
        selectedRegion: FeishuPlatformRegion,
        onRegionChange: (region: FeishuPlatformRegion) => void,
    ) => {
        return (
            <div className="channel-config-selector-row">
                <label className="channel-config-selector-label">
                    {t('agent.settings.channel.platform', 'Platform')}
                </label>
                <select
                    className={`${mode === 'edit' ? 'input' : 'form-input'} channel-config-selector-select`}
                    value={selectedRegion}
                    onChange={event => onRegionChange(normalizeFeishuPlatformRegion(event.target.value))}
                    aria-label={t('agent.settings.channel.platform', 'Platform')}
                >
                    {FEISHU_PLATFORM_OPTIONS.map(option => (
                        <option key={`${channelId}-${option.value}`} value={option.value}>
                            {t(option.labelKey, option.fallback)}
                        </option>
                    ))}
                </select>
            </div>
        );
    };

    // ─── Render create mode channel card ─────────────────
    const renderCreateChannel = (ch: ChannelDef) => {
        const isOpen = openChannels[ch.id] || false;

        // Ensure we default to 'websocket' for connectionMode in create view if enabled
        const connMode = ch.connectionMode ? (connectionModes[ch.id] || 'websocket') : null;
        const isWs = connMode === 'websocket';
        const selectedFeishuPlatformRegion = normalizeFeishuPlatformRegion(
            values?.[`${ch.id}_platform_region`] || feishuPlatformRegions[ch.id],
        );
        
        // Active fields for current mode
        const activeFields = (ch.connectionMode && isWs && ch.wsFields) ? ch.wsFields : ch.fields;
        
        // Special Feishu field filtering (hide encrypt_key if websocket mode)
        const formFields = ch.id === 'feishu' && isWs
            ? ch.fields.filter(f => f.key !== 'encrypt_key')
            : activeFields;

        // Determine if configured (any required field has value)
        const hasValues = formFields.some(f => f.required && values?.[`${ch.id}_${f.key}`]);

        let subtitle = ch.desc;
        if (ch.connectionMode && hasValues) {
            subtitle = isWs ? 'WebSocket Mode' : 'Webhook Mode';
        }

        return (
            <div key={ch.id} className="channel-config-create-card">
                <div
                    onClick={() => toggleChannel(ch.id)}
                    className={`channel-config-create-header${isOpen ? ' is-open' : ''}`}
                >
                    {ch.icon}
                    <div className="channel-config-flex-1">
                        <div className="channel-config-create-name">{t(ch.nameKey, ch.nameFallback)}</div>
                        <div className="channel-config-create-desc">{subtitle}</div>
                    </div>
                    {hasValues && <span className="channel-config-configured-pill">{t('agent.settings.channel.configured', 'Configured')}</span>}
                    <span className={`channel-config-chevron${isOpen ? ' is-open' : ''}`}>&#9660;</span>
                </div>
                {isOpen && (
                    <div className="channel-config-create-body">
                        {ch.id === 'feishu' && renderFeishuPlatformSelector(ch.id, selectedFeishuPlatformRegion, region => {
                            setFeishuPlatformRegions(prev => ({ ...prev, [ch.id]: region }));
                            onChange?.({
                                ...values,
                                [`${ch.id}_platform_region`]: region,
                                [`${ch.id}_connection_mode`]: connMode || 'websocket',
                            });
                        })}

                        {/* Connection Mode Toggle */}
                        {ch.connectionMode && (
                            <div className="channel-config-selector-row">
                                <label className="channel-config-selector-label">{t('agent.settings.channel.mode', 'Connection Mode')}</label>
                                <label className="channel-config-radio">
                                    <input type="radio" checked={isWs} onChange={() => setConnectionModes(p => ({ ...p, [ch.id]: 'websocket' }))} />
                                    {t('agent.settings.channel.modeWs', 'WebSocket (Recommended)')}
                                </label>
                                <label className="channel-config-radio channel-config-radio-spaced">
                                    <input type="radio" checked={!isWs} onChange={() => setConnectionModes(p => ({ ...p, [ch.id]: 'webhook' }))} />
                                    {t('agent.settings.channel.modeWebhook', 'Webhook')}
                                </label>
                            </div>
                        )}
                        
                        {renderGuide(ch.guide, !!isWs, ch)}
                        
                        {formFields.map(field => (
                            <div className="form-group" key={field.key}>
                                {renderField(
                                    field, ch.id,
                                    values?.[`${ch.id}_${field.key}`] || '',
                                    (val) => {
                                        const newValues = { ...values, [`${ch.id}_${field.key}`]: val };
                                        // Save connection mode if this channel supports it
                                        if (ch.connectionMode) {
                                            newValues[`${ch.id}_connection_mode`] = connMode || 'websocket';
                                        }
                                        if (ch.id === 'feishu') {
                                            newValues[`${ch.id}_platform_region`] = selectedFeishuPlatformRegion;
                                        }
                                        onChange?.(newValues);
                                    },
                                )}
                            </div>
                        ))}
                    </div>
                )}
            </div>
        );
    };

    // ─── Render edit mode channel card ───────────────────
    const renderEditChannel = (ch: ChannelDef) => {
        const config = getConfig(ch.id);
        const webhook = getWebhook(ch.id);
        const isOpen = openChannels[ch.id] || false;
        const isEditing = editingChannels[ch.id] || false;
        const form = getForm(ch.id);
        const isConfigured = ch.id === 'wechat_personal'
            ? Boolean(
                config?.connected
                && config?.identity_status === 'verified'
                && !config?.requires_rebind,
            )
            : Boolean(config?.is_configured);
        const requiresRebind = ch.id === 'wechat_personal' && Boolean(
            config?.requires_rebind
            || (config?.connected && config?.identity_status !== 'verified'),
        );
        const requiresAccessRecovery = ch.id === 'wechat_personal' && Boolean(
            config?.requires_access_recovery
            || config?.identity_status === 'access_denied',
        );
        const connMode = connectionModes[ch.id] || 'websocket';
        const isWs = ch.connectionMode && connMode === 'websocket';
        const configConnMode = config?.extra_config?.connection_mode;
        const feishuConnectionStatus = String(config?.extra_config?.connection_status || '');
        const feishuWebSocketConnected = ch.id === 'feishu'
            && configConnMode === 'websocket'
            && Boolean(config?.is_connected);
        const feishuWebSocketFailed = ch.id === 'feishu'
            && configConnMode === 'websocket'
            && (!config?.is_configured || feishuConnectionStatus === 'invalid_credentials');
        const feishuWebSocketConnecting = ch.id === 'feishu'
            && configConnMode === 'websocket'
            && !feishuWebSocketConnected
            && !feishuWebSocketFailed
            && ['connecting', 'transient_error'].includes(feishuConnectionStatus);
        const selectedFeishuPlatformRegion = normalizeFeishuPlatformRegion(feishuPlatformRegions[ch.id]);
        const currentConfigFeishuPlatformRegion = normalizeFeishuPlatformRegion(config?.extra_config?.platform_region);
        const currentConfigFeishuPlatformOption = getFeishuPlatformOption(currentConfigFeishuPlatformRegion);

        // Determine desc subtitle based on current mode
        let subtitle = ch.desc;
        if (ch.connectionMode && config) {
            subtitle = configConnMode === 'websocket' ? 'WebSocket Mode' : ch.desc;
        }

        // Webhook URL for this channel
        const webhookUrl = webhook?.webhook_url || `${window.location.origin}/api/channel/${ch.id === 'feishu' ? 'feishu' : ch.apiSlug?.replace('-channel', '')}/${agentId}/webhook`;

        // Determine which fields to use (wecom websocket mode has different fields)
        const activeFields = (ch.connectionMode && isWs && ch.wsFields) ? ch.wsFields : ch.fields;
        // For feishu, hide encrypt_key in websocket mode (non-editing form)
        const formFields = ch.id === 'feishu' && connMode === 'webhook'
            ? ch.fields
            : ch.id === 'feishu'
                ? ch.fields.filter(f => f.key !== 'encrypt_key')
                : activeFields;

        // Check if all required fields are filled
        const allRequired = formFields.filter(f => f.required).every(f => form[f.key]);

        return (
            <div key={ch.id} className="channel-config-edit-card">
                {/* Header */}
                <div onClick={() => toggleChannel(ch.id)}
                    className="channel-config-edit-header">
                    <div className="channel-config-hstack">
                        {ch.icon}
                        <div>
                            <div className="channel-config-edit-name">{t(ch.nameKey, ch.nameFallback)}</div>
                            <div className="channel-config-create-desc">{subtitle}</div>
                        </div>
                    </div>
                    <div className="channel-config-hstack">
                        {config && (
                            <span className={`badge ${
                                ch.id === 'feishu' && configConnMode === 'websocket'
                                    ? feishuWebSocketConnected ? 'badge-success' : 'badge-warning'
                                    : isConfigured ? 'badge-success' : 'badge-warning'
                            }`}>
                                {requiresAccessRecovery
                                    ? t('agent.settings.channel.accessRecoveryRequired', 'Bound account access lost')
                                    : requiresRebind
                                    ? t('agent.settings.channel.rebindRequired', 'Rebind required')
                                    : ch.id === 'feishu' && configConnMode === 'websocket'
                                        ? feishuWebSocketConnected
                                            ? t('agent.settings.channel.registration.statusConnected', 'WebSocket connected')
                                            : feishuWebSocketFailed
                                                ? t('agent.settings.channel.registration.statusError', 'WebSocket connection failed; scan again')
                                                : feishuWebSocketConnecting
                                                    ? t('agent.settings.channel.registration.statusConnecting', 'App created; connecting WebSocket')
                                                    : t('agent.settings.channel.registration.statusDisconnected', 'App configured, but WebSocket is not connected')
                                    : isConfigured
                                        ? t('agent.settings.channel.configured')
                                        : t('agent.settings.channel.notConfigured')}
                            </span>
                        )}
                        <span className={`channel-config-chevron${isOpen ? ' is-open' : ''}`}>&#9660;</span>
                    </div>
                </div>

                {/* Body */}
                {isOpen && (
                    <div className="channel-config-edit-body">
                        {!canManage ? (
                            <div className="channel-config-note">
                                Only the creator or admin can configure communication channels.
                            </div>
                        ) : ch.qrScanMode ? (
                            /* ── QR Scan mode (Personal WeChat) ── */
                            <WeChatPersonalSetup
                                agentId={agentId!}
                                onConnected={() => {
                                    queryClient.invalidateQueries({ queryKey: ['wechat-personal-status', agentId] });
                                }}
                            />
                        ) : ch.id === 'feishu' && !isEditing && (!isConfigured || feishuQrOpen) ? (
                            <div className="channel-config-vstack">
                                {renderFeishuPlatformSelector(ch.id, selectedFeishuPlatformRegion, region => {
                                    setFeishuPlatformRegions(prev => ({ ...prev, [ch.id]: region }));
                                })}
                                <FeishuAppRegistrationSetup
                                    agentId={agentId!}
                                    platformRegion={selectedFeishuPlatformRegion}
                                    onConnected={() => {
                                        queryClient.invalidateQueries({ queryKey: ['channel', agentId] });
                                        setFeishuQrOpen(false);
                                    }}
                                    onManualConfigure={() => {
                                        setFeishuQrOpen(false);
                                        setEditing(ch.id, true);
                                    }}
                                    onClose={isConfigured ? () => setFeishuQrOpen(false) : undefined}
                                />
                            </div>
                        ) : isConfigured && !isEditing ? (
                            /* ── Configured view ── */
                            <div>
                                {/* Feishu websocket status */}
                                {ch.id === 'feishu' && configConnMode === 'websocket' && (
                                    <div className="channel-config-status-box">
                                        <div className="channel-config-status-line">
                                            <span className={`channel-config-dot${
                                                feishuWebSocketConnected
                                                    ? ''
                                                    : feishuWebSocketFailed
                                                        ? ' is-error'
                                                        : ' is-warning'
                                            }`}></span>
                                            <span className="u-secondary">
                                                {feishuWebSocketConnected
                                                    ? t('agent.settings.channel.registration.statusConnected', 'WebSocket connected')
                                                    : feishuWebSocketFailed
                                                        ? t('agent.settings.channel.registration.statusError', 'WebSocket connection failed; scan again')
                                                        : feishuWebSocketConnecting
                                                            ? t('agent.settings.channel.registration.statusConnecting', 'App created; connecting WebSocket')
                                                            : t('agent.settings.channel.registration.statusDisconnected', 'App configured, but WebSocket is not connected')}
                                            </span>
                                        </div>
                                        <div className="channel-config-meta-line">
                                            {t('agent.settings.channel.platform', 'Platform')}: <strong>{t(currentConfigFeishuPlatformOption.labelKey, currentConfigFeishuPlatformOption.fallback)}</strong>
                                        </div>
                                        <div className="channel-config-hint">App ID: <code>{config.app_id}</code></div>
                                    </div>
                                )}
                                {ch.id === 'feishu' && configConnMode !== 'websocket' && (
                                    <div className="channel-config-info-block">
                                        <div className="channel-config-mb-1">
                                            {t('agent.settings.channel.platform', 'Platform')}: <strong>{t(currentConfigFeishuPlatformOption.labelKey, currentConfigFeishuPlatformOption.fallback)}</strong>
                                        </div>
                                        <div className="channel-config-mb-1">Mode: <strong>Webhook</strong></div>
                                        <div>App ID: <code>{config.app_id}</code></div>
                                    </div>
                                )}

                                {/* WeCom websocket status */}
                                {ch.id === 'wecom' && configConnMode === 'websocket' && (
                                    <div className="channel-config-status-box">
                                        <div className="channel-config-hstack-sm">
                                            <span className="channel-config-dot"></span>
                                            <span className="u-secondary">Connected via WebSocket (No callback URL needed)</span>
                                        </div>
                                    </div>
                                )}

                                {/* Webhook URL (non-websocket channels) */}
                                {ch.webhookLabel && !(ch.connectionMode && configConnMode === 'websocket') && ch.id !== 'dingtalk' && (
                                    <div className="channel-config-webhook-box">
                                        <div className="channel-config-webhook-label">{ch.webhookLabel}</div>
                                        <div className="channel-config-webhook-url">
                                            <span className="channel-config-webhook-link">{webhookUrl}</span>
                                            <CopyBtn url={webhookUrl} />
                                        </div>
                                    </div>
                                )}

                                {/* Discord extra hint */}
                                {ch.id === 'discord' && configConnMode !== 'gateway' && (
                                    <div className="channel-config-hint channel-config-mb-2">Use <code>/ask message:&lt;your question&gt;</code> to talk to this agent</div>
                                )}
                                {ch.id === 'discord' && configConnMode === 'gateway' && (
                                    <div className="channel-config-status-box">
                                        <div className="channel-config-status-line">
                                            <span className="channel-config-dot"></span>
                                            <span className="u-secondary">Connected via Gateway (No public URL needed)</span>
                                        </div>
                                        <div className="channel-config-hint">@mention the bot or send a DM to interact</div>
                                    </div>
                                )}

                                {/* DingTalk stream mode hint */}
                                {ch.id === 'dingtalk' && (
                                    <div className="channel-config-hint-box">
                                        Stream mode active. No webhook URL needed.
                                    </div>
                                )}

                                {/* AgentBay status */}
                                {ch.id === 'agentbay' && (
                                    <div className="channel-config-status-box">
                                        <div className="channel-config-webhook-label">Status</div>
                                        <div className="channel-config-status-value">API Key configured — Browser & Code tools available</div>
                                        {config.base_url && <div className="channel-config-status-sub">Base URL: <code>{config.base_url}</code></div>}
                                    </div>
                                )}
                                {ch.id === 'agentbay' && agentbayTestResult && (
                                    <div className={`channel-config-test-result ${agentbayTestResult.ok ? 'is-ok' : 'is-error'}`}>
                                        {agentbayTestResult.ok
                                            ? `${agentbayTestResult.message || 'Connected to AgentBay'}`
                                            : `${agentbayTestResult.error}`}
                                    </div>
                                )}

                                {/* Setup guide in configured view */}
                                {renderGuide(ch.guide, !!(ch.connectionMode && configConnMode === 'websocket'), ch)}

                                {/* Action buttons */}
                                <div className="channel-config-actions">
                                    {ch.hasTestConnection && ch.id === 'agentbay' && (
                                        <button className="btn btn-secondary" onClick={testAgentBay} disabled={agentbayTesting}>
                                            {agentbayTesting ? 'Testing...' : 'Test Connection'}
                                        </button>
                                    )}
                                    {ch.id === 'feishu' && (
                                        <button
                                            type="button"
                                            className="btn btn-primary"
                                            onClick={() => {
                                                setFeishuPlatformRegions(prev => ({
                                                    ...prev,
                                                    feishu: currentConfigFeishuPlatformRegion,
                                                }));
                                                setFeishuQrOpen(true);
                                            }}
                                        >
                                            {t('agent.settings.channel.registration.reconnect', 'Reconnect by QR code')}
                                        </button>
                                    )}
                                    <button className="btn btn-secondary"
                                        onClick={() => {
                                            // Populate form with existing config data
                                            const prefill: Record<string, string> = {};
                                            if (ch.id === 'feishu') {
                                                prefill.app_id = config.app_id || '';
                                                prefill.app_secret = config.app_secret || '';
                                                prefill.encrypt_key = config.encrypt_key || '';
                                                setConnectionModes(prev => ({ ...prev, feishu: config.extra_config?.connection_mode || 'websocket' }));
                                                setFeishuPlatformRegions(prev => ({
                                                    ...prev,
                                                    feishu: normalizeFeishuPlatformRegion(config.extra_config?.platform_region),
                                                }));
                                            } else if (ch.id === 'wecom') {
                                                const cm = config.extra_config?.connection_mode === 'websocket' ? 'websocket' : 'webhook';
                                                setConnectionModes(prev => ({ ...prev, wecom: cm }));
                                                if (cm === 'websocket') {
                                                    prefill.bot_id = config.extra_config?.bot_id || '';
                                                    prefill.bot_secret = config.extra_config?.bot_secret || '';
                                                } else {
                                                    prefill.corp_id = config.app_id || '';
                                                    prefill.wecom_agent_id = config.extra_config?.wecom_agent_id || '';
                                                    prefill.secret = config.app_secret || '';
                                                    prefill.token = config.verification_token || '';
                                                    prefill.encoding_aes_key = config.encrypt_key || '';
                                                }
                                            } else if (ch.id === 'slack') {
                                                prefill.bot_token = config.app_secret || '';
                                                prefill.signing_secret = config.encrypt_key || '';
                                            } else if (ch.id === 'discord') {
                                                const cm = config.extra_config?.connection_mode === 'gateway' ? 'websocket' : 'webhook';
                                                setConnectionModes(prev => ({ ...prev, discord: cm }));
                                                if (cm === 'websocket') {
                                                    prefill.bot_token = config.app_secret || '';
                                                } else {
                                                    prefill.application_id = config.app_id || '';
                                                    prefill.bot_token = config.app_secret || '';
                                                    prefill.public_key = config.encrypt_key || '';
                                                }
                                            } else if (ch.id === 'teams') {
                                                prefill.app_id = config.app_id || '';
                                                prefill.app_secret = config.app_secret || '';
                                                prefill.tenant_id = config.extra_config?.tenant_id || '';
                                            } else if (ch.id === 'dingtalk') {
                                                prefill.app_key = config.app_id || '';
                                                prefill.app_secret = config.app_secret || '';
                                            } else if (ch.id === 'agentbay') {
                                                prefill.api_key = '';
                                                prefill.base_url = config.base_url || '';
                                            }
                                            setForms(prev => ({ ...prev, [ch.id]: prefill }));
                                            setEditing(ch.id, true);
                                        }}>
                                        {ch.id === 'feishu'
                                            ? t('agent.settings.channel.registration.manual', 'Manual configuration (advanced)')
                                            : 'Edit'}
                                    </button>
                                    <button className="btn btn-danger"
                                        onClick={() => deleteMutation.mutate({ ch })}>Disconnect</button>
                                </div>
                            </div>
                        ) : (
                            /* ── Form view (new or editing) ── */
                            <div className="channel-config-vstack">
                                {ch.id === 'feishu' && renderFeishuPlatformSelector(ch.id, selectedFeishuPlatformRegion, region => {
                                    setFeishuPlatformRegions(prev => ({ ...prev, [ch.id]: region }));
                                })}

                                {/* Connection mode toggle (feishu, wecom) */}
                                {ch.connectionMode && (
                                    <div className="channel-config-mb-2">
                                        <label className="channel-config-form-label">{t('wizard.step5.connectionMode')}</label>
                                        <div className="channel-config-radio-group">
                                            <label className="channel-config-radio-inline">
                                                <input type="radio" name={`${ch.id}_connection_mode`} value="websocket" checked={connMode === 'websocket'}
                                                    onChange={() => setConnectionModes(prev => ({ ...prev, [ch.id]: 'websocket' }))} />
                                                {t('wizard.step5.modeWebsocket')}
                                            </label>
                                            <label className="channel-config-radio-inline">
                                                <input type="radio" name={`${ch.id}_connection_mode`} value="webhook" checked={connMode === 'webhook'}
                                                    onChange={() => setConnectionModes(prev => ({ ...prev, [ch.id]: 'webhook' }))} />
                                                {t('wizard.step5.modeWebhook')}
                                            </label>
                                        </div>
                                    </div>
                                )}

                                {renderGuide(ch.guide, !!isWs, ch)}

                                {/* Form fields */}
                                {formFields.map(field =>
                                    renderField(field, ch.id, form[field.key] || '', (val) => setFormField(ch.id, field.key, val))
                                )}

                                {/* AgentBay extra hints */}
                                {ch.id === 'agentbay' && (
                                    <>
                                        <div className="channel-config-hint-tight">
                                            Get your API key from <a href="https://www.aliyun.com/product/agentbay" target="_blank" rel="noopener noreferrer" className="channel-config-link">Aliyun AgentBay Console</a>
                                        </div>
                                        <div className="channel-config-hint">Leave Base URL empty to use the default endpoint</div>
                                    </>
                                )}

                                {/* Save / Cancel buttons */}
                                <div className="channel-config-actions-mt">
                                    <button className="btn btn-primary channel-config-btn-start"
                                        onClick={() => {
                                            const payload = buildPayload(ch, form);
                                            saveMutation.mutate({ ch, data: payload });
                                        }}
                                        disabled={!allRequired || saveMutation.isPending}>
                                        {saveMutation.isPending ? t('common.loading') : (isEditing ? 'Save Changes' : t('agent.settings.channel.saveChannel'))}
                                    </button>
                                    {isEditing && <button className="btn btn-secondary" onClick={() => setEditing(ch.id, false)}>Cancel</button>}
                                </div>
                            </div>
                        )}
                    </div>
                )}
            </div>
        );
    };

    // ─── Render ─────────────────────────────────────────
    if (mode === 'create') {
        return (
            <div className="channel-config-vstack">
                {/* Configurable channels */}
                {CHANNEL_REGISTRY.filter(ch => !ch.editOnly).map(renderCreateChannel)}

                {/* Disabled channels: configure in settings after creation */}
                {CHANNEL_REGISTRY.filter(ch => ch.editOnly).map(ch => (
                    <div key={ch.id} className="channel-config-disabled-card">
                        {ch.icon}
                        <div className="channel-config-flex-1">
                            <div className="channel-config-create-name">{t(ch.nameKey, ch.nameFallback)}</div>
                            <div className="channel-config-create-desc">{ch.desc}</div>
                        </div>
                        <span className="channel-config-settings-pill">Configure in Settings</span>
                    </div>
                ))}
            </div>
        );
    }

    // Edit mode
    return (
        <div className="card channel-config-mb-3">
            <h4 className="channel-config-mb-3">{t('agent.settings.channel.title')}</h4>
            <p className="channel-config-subtitle">{t('agent.settings.channel.title')}</p>
            <div className="channel-config-sync-hint">
                {t('agent.settings.channel.syncHint', 'Before configuring the Feishu bot, please sync your organization structure in Enterprise Settings → Org Structure first. This ensures the bot can identify message senders.')}
            </div>
            {feishuRuntimeStatus ? (
                <div className="channel-config-mb-4">
                    <FeishuRuntimeStatusCard status={feishuRuntimeStatus} />
                </div>
            ) : null}
            {VISIBLE_CHANNELS.map(renderEditChannel)}
        </div>
    );
}
