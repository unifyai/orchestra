--
--

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

--
-- Name: safe_cast_to_date(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.safe_cast_to_date(input_text text) RETURNS date
    LANGUAGE plpgsql IMMUTABLE
    AS $$
        BEGIN
            RETURN input_text::DATE;
        EXCEPTION
            WHEN OTHERS THEN
                RETURN NULL;
        END;
        $$;

--
-- Name: safe_cast_to_interval(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.safe_cast_to_interval(input_text text) RETURNS interval
    LANGUAGE plpgsql IMMUTABLE
    AS $$
        BEGIN
            RETURN input_text::INTERVAL;
        EXCEPTION
            WHEN OTHERS THEN
                RETURN NULL;
        END;
        $$;

--
-- Name: safe_cast_to_time(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.safe_cast_to_time(input_text text) RETURNS time without time zone
    LANGUAGE plpgsql IMMUTABLE
    AS $$
        BEGIN
            RETURN input_text::TIME;
        EXCEPTION
            WHEN OTHERS THEN
                RETURN NULL;
        END;
        $$;

--
-- Name: safe_cast_to_timestamptz(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.safe_cast_to_timestamptz(input_text text) RETURNS timestamp with time zone
    LANGUAGE plpgsql IMMUTABLE
    AS $$
        BEGIN
            RETURN input_text::TIMESTAMP WITH TIME ZONE;
        EXCEPTION
            WHEN OTHERS THEN
                RETURN NULL;
        END;
        $$;

--
-- Name: account; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account (
    id character varying NOT NULL,
    user_id character varying,
    provider character varying NOT NULL,
    provider_type character varying NOT NULL,
    provider_account_id character varying NOT NULL,
    access_token character varying,
    refresh_token character varying,
    expires_at timestamp without time zone
);

--
-- Name: admin_user; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_user (
    id integer NOT NULL,
    user_id character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone
);

--
-- Name: admin_user_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.admin_user_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: admin_user_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.admin_user_id_seq OWNED BY public.admin_user.id;

--
-- Name: api_key; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_key (
    id integer NOT NULL,
    name character varying,
    user_id character varying,
    organization_id integer,
    key character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now()
);

--
-- Name: api_key_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.api_key_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: api_key_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.api_key_id_seq OWNED BY public.api_key.id;

--
-- Name: api_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_messages (
    id character varying NOT NULL,
    assistant_id integer NOT NULL,
    user_id character varying NOT NULL,
    organization_id integer,
    message character varying NOT NULL,
    status character varying DEFAULT 'processing'::character varying NOT NULL,
    response character varying,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    completed_at timestamp without time zone,
    tags jsonb DEFAULT '[]'::jsonb,
    attachments jsonb DEFAULT '[]'::jsonb,
    response_tags jsonb,
    response_attachments jsonb
);

--
-- Name: assistant_cleanup_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assistant_cleanup_tasks (
    id integer NOT NULL,
    assistant_id integer NOT NULL,
    deploy_env character varying,
    desktop_mode character varying,
    source_flow character varying NOT NULL,
    cleanup_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    status character varying DEFAULT 'pending'::character varying NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    last_error character varying,
    last_result jsonb,
    next_retry_at timestamp with time zone,
    processing_started_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    completed_at timestamp with time zone,
    CONSTRAINT ck_assistant_cleanup_task_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'processing'::character varying, 'completed'::character varying, 'failed'::character varying])::text[])))
);

--
-- Name: assistant_cleanup_tasks_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.assistant_cleanup_tasks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: assistant_cleanup_tasks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.assistant_cleanup_tasks_id_seq OWNED BY public.assistant_cleanup_tasks.id;

--
-- Name: assistant_console_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assistant_console_config (
    id integer NOT NULL,
    assistant_id integer NOT NULL,
    version character varying DEFAULT '1'::character varying NOT NULL,
    layout_mode character varying DEFAULT 'standard'::character varying NOT NULL,
    layout_default_tab character varying,
    tabs_hidden jsonb,
    tabs_order jsonb,
    theme_brand_name character varying,
    theme_accent_color character varying,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now()
);

--
-- Name: assistant_console_config_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.assistant_console_config_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: assistant_console_config_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.assistant_console_config_id_seq OWNED BY public.assistant_console_config.id;

--
-- Name: assistant_contacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assistant_contacts (
    id integer NOT NULL,
    assistant_id integer NOT NULL,
    contact_type character varying NOT NULL,
    contact_value character varying NOT NULL,
    provider character varying,
    provisioned_by character varying DEFAULT 'platform'::character varying NOT NULL,
    country_code character varying,
    status character varying DEFAULT 'active'::character varying NOT NULL,
    metadata jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    deleted_at timestamp with time zone,
    grace_period_started_at timestamp with time zone,
    last_billed_month character varying,
    monthly_cost numeric,
    CONSTRAINT ck_assistant_contact_provisioned_by CHECK (((provisioned_by)::text = ANY ((ARRAY['platform'::character varying, 'user'::character varying])::text[]))),
    CONSTRAINT ck_assistant_contact_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'grace_period'::character varying, 'deleted'::character varying])::text[]))),
    CONSTRAINT ck_assistant_contact_type CHECK (((contact_type)::text = ANY ((ARRAY['phone'::character varying, 'email'::character varying, 'whatsapp'::character varying, 'discord'::character varying])::text[])))
);

--
-- Name: assistant_contacts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.assistant_contacts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: assistant_contacts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.assistant_contacts_id_seq OWNED BY public.assistant_contacts.id;

--
-- Name: assistant_secrets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assistant_secrets (
    user_id character varying NOT NULL,
    agent_id integer NOT NULL,
    secret_name character varying NOT NULL,
    secret_value character varying NOT NULL,
    description character varying,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now()
);

--
-- Name: assistant_space_memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assistant_space_memberships (
    assistant_id integer NOT NULL,
    space_id bigint NOT NULL,
    added_by character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: assistants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assistants (
    agent_id integer NOT NULL,
    user_id character varying NOT NULL,
    first_name character varying,
    surname character varying,
    age integer,
    weekly_limit numeric,
    max_parallel integer,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    nationality character varying,
    profile_photo character varying,
    about character varying,
    voice_id character varying,
    profile_video character varying,
    voice_provider character varying,
    timezone character varying,
    organization_id integer,
    desktop_mode character varying,
    monthly_spending_cap numeric,
    user_desktop_filesys_sync boolean DEFAULT false NOT NULL,
    monthly_spending_cap_set_at timestamp with time zone,
    user_desktop_id integer,
    is_local boolean DEFAULT false NOT NULL,
    desktop_filesync_sshkey character varying,
    deploy_env character varying,
    job_title character varying,
    last_correspondence_at timestamp with time zone DEFAULT now(),
    last_followup_sent_at timestamp with time zone,
    termination_initiated_at timestamp with time zone,
    is_coordinator boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_assistant_desktop_mode CHECK (((desktop_mode)::text = ANY ((ARRAY['ubuntu'::character varying, 'windows'::character varying, 'macos'::character varying])::text[])))
);

--
-- Name: assistants_agent_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.assistants_agent_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: assistants_agent_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.assistants_agent_id_seq OWNED BY public.assistants.agent_id;

--
-- Name: auth_rate_limit_entry; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_rate_limit_entry (
    id integer NOT NULL,
    key character varying(500) NOT NULL,
    endpoint_category character varying(50) NOT NULL,
    time_bucket timestamp with time zone NOT NULL,
    attempt_count integer DEFAULT 1 NOT NULL
);

--
-- Name: auth_rate_limit_entry_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.auth_rate_limit_entry_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: auth_rate_limit_entry_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.auth_rate_limit_entry_id_seq OWNED BY public.auth_rate_limit_entry.id;

--
-- Name: billing_account; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.billing_account (
    id integer NOT NULL,
    credits numeric DEFAULT '0'::numeric NOT NULL,
    stripe_customer_id character varying,
    autorecharge boolean DEFAULT false NOT NULL,
    autorecharge_threshold numeric DEFAULT '0'::numeric NOT NULL,
    autorecharge_qty numeric DEFAULT '25'::numeric NOT NULL,
    account_status character varying DEFAULT 'ACTIVE'::character varying NOT NULL,
    billing_setup_complete boolean DEFAULT false NOT NULL,
    tier character varying DEFAULT 'developer'::character varying NOT NULL,
    billing_email character varying,
    name character varying(255),
    tax_id character varying(100),
    tax_id_type character varying(50),
    tax_id_verification_status character varying(20),
    billing_address jsonb,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone,
    suspension_reason character varying,
    plan_assignment_id bigint,
    preferred_payment_method_types character varying[],
    plan_group_id bigint DEFAULT 1 NOT NULL,
    CONSTRAINT ck_billing_account_status CHECK (((account_status)::text = ANY ((ARRAY['ACTIVE'::character varying, 'SUSPENDED'::character varying, 'CLOSED'::character varying])::text[])))
);

--
-- Name: billing_account_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.billing_account_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: billing_account_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.billing_account_id_seq OWNED BY public.billing_account.id;

--
-- Name: billing_plan_assignment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.billing_plan_assignment (
    id bigint NOT NULL,
    billing_account_id integer NOT NULL,
    template_id bigint NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    created_by_user_id character varying,
    change_reason text,
    CONSTRAINT ck_billing_plan_assignment_window CHECK (((ended_at IS NULL) OR (ended_at >= started_at)))
);

--
-- Name: billing_plan_assignment_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.billing_plan_assignment_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: billing_plan_assignment_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.billing_plan_assignment_id_seq OWNED BY public.billing_plan_assignment.id;

--
-- Name: billing_plan_template; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.billing_plan_template (
    id bigint NOT NULL,
    name character varying(120) NOT NULL,
    display_name character varying(120),
    description text,
    billing_mode character varying DEFAULT 'CREDITS'::character varying NOT NULL,
    commit_amount numeric,
    currency character varying(3) DEFAULT 'USD'::character varying NOT NULL,
    commit_period character varying,
    commit_schedule character varying,
    base_pricing_factor numeric DEFAULT 1.0 NOT NULL,
    overage_pricing_factor numeric DEFAULT 1.0 NOT NULL,
    collection_method character varying DEFAULT 'AUTO_CARD'::character varying NOT NULL,
    proration_policy character varying DEFAULT 'PRORATE'::character varying NOT NULL,
    credits_rollover_policy character varying,
    fx_policy character varying(32),
    fx_locked_rate numeric(18,8),
    is_custom boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    supersedes_template_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by_user_id character varying,
    CONSTRAINT ck_plan_template_billing_mode CHECK (((billing_mode)::text = ANY ((ARRAY['CREDITS'::character varying, 'METERED'::character varying])::text[]))),
    CONSTRAINT ck_plan_template_collection_method CHECK (((collection_method)::text = ANY ((ARRAY['AUTO_CARD'::character varying, 'SEND_INVOICE_NET_30'::character varying])::text[]))),
    CONSTRAINT ck_plan_template_commit_has_period CHECK (((commit_amount IS NULL) OR (commit_amount = (0)::numeric) OR (commit_period IS NOT NULL))),
    CONSTRAINT ck_plan_template_commit_period CHECK (((commit_period IS NULL) OR ((commit_period)::text = ANY ((ARRAY['MONTHLY'::character varying, 'QUARTERLY'::character varying, 'ANNUAL'::character varying])::text[])))),
    CONSTRAINT ck_plan_template_commit_schedule CHECK (((commit_schedule IS NULL) OR ((commit_schedule)::text = ANY ((ARRAY['AMORTISED'::character varying, 'UPFRONT'::character varying])::text[])))),
    CONSTRAINT ck_plan_template_credits_rollover_policy CHECK (((credits_rollover_policy IS NULL) OR ((credits_rollover_policy)::text = ANY ((ARRAY['ROLL_OVER'::character varying, 'FORFEIT_AT_PERIOD_END'::character varying])::text[])))),
    CONSTRAINT ck_plan_template_credits_rollover_scope CHECK (((credits_rollover_policy IS NULL) OR ((commit_amount IS NOT NULL) AND (commit_amount > (0)::numeric) AND ((billing_mode)::text = 'CREDITS'::text)))),
    CONSTRAINT ck_plan_template_fx_locked_rate CHECK (((((fx_policy)::text = 'LOCKED_RATE'::text) AND (fx_locked_rate IS NOT NULL) AND (fx_locked_rate > (0)::numeric)) OR (((fx_policy)::text IS DISTINCT FROM 'LOCKED_RATE'::text) AND (fx_locked_rate IS NULL)))),
    CONSTRAINT ck_plan_template_fx_policy CHECK (((fx_policy IS NULL) OR ((fx_policy)::text = ANY ((ARRAY['LOCKED_RATE'::character varying, 'SPOT'::character varying, 'PERIOD_AVERAGE'::character varying])::text[])))),
    CONSTRAINT ck_plan_template_fx_required_for_non_usd CHECK (((((currency)::text = 'USD'::text) AND (fx_policy IS NULL)) OR (((currency)::text <> 'USD'::text) AND (fx_policy IS NOT NULL)))),
    CONSTRAINT ck_plan_template_pricing_factors_positive CHECK (((base_pricing_factor > (0)::numeric) AND (overage_pricing_factor > (0)::numeric))),
    CONSTRAINT ck_plan_template_proration_policy CHECK (((proration_policy)::text = ANY ((ARRAY['PRORATE'::character varying, 'SKIP_FIRST'::character varying, 'FULL_FIRST'::character varying])::text[]))),
    CONSTRAINT ck_plan_template_upfront_no_period_average_fx CHECK ((((commit_schedule)::text IS DISTINCT FROM 'UPFRONT'::text) OR (fx_policy IS NULL) OR ((fx_policy)::text = ANY ((ARRAY['LOCKED_RATE'::character varying, 'SPOT'::character varying])::text[])))),
    CONSTRAINT ck_plan_template_upfront_requires_full_first CHECK ((((commit_schedule)::text IS DISTINCT FROM 'UPFRONT'::text) OR ((proration_policy)::text = 'FULL_FIRST'::text)))
);

--
-- Name: billing_plan_template_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.billing_plan_template_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: billing_plan_template_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.billing_plan_template_id_seq OWNED BY public.billing_plan_template.id;

--
-- Name: conflict_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.conflict_events (
    id integer NOT NULL,
    platform character varying NOT NULL,
    conflict_type character varying NOT NULL,
    trigger_assistant_id integer,
    affected_assistant_ids jsonb NOT NULL,
    old_pool_assignments jsonb NOT NULL,
    new_pool_assignments jsonb NOT NULL,
    notification_recipients jsonb,
    notification_status jsonb,
    status character varying DEFAULT 'notifying'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    resolved_at timestamp with time zone,
    CONSTRAINT ck_conflict_event_status CHECK (((status)::text = ANY ((ARRAY['notifying'::character varying, 'resolved'::character varying, 'notification_failed'::character varying, 'failed'::character varying])::text[]))),
    CONSTRAINT ck_conflict_event_type CHECK (((conflict_type)::text = ANY ((ARRAY['contact_overlap'::character varying, 'user_to_user'::character varying, 'org_membership'::character varying])::text[])))
);

--
-- Name: conflict_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.conflict_events_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: conflict_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.conflict_events_id_seq OWNED BY public.conflict_events.id;

--
-- Name: contact_memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_memberships (
    id bigint NOT NULL,
    assistant_id integer NOT NULL,
    contact_id integer NOT NULL,
    target_scope text NOT NULL,
    target_space_id bigint,
    relationship text NOT NULL,
    should_respond boolean DEFAULT true NOT NULL,
    response_policy text DEFAULT 'standard'::text NOT NULL,
    can_edit boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    authoring_assistant_id integer,
    CONSTRAINT ck_contact_memberships_relationship CHECK ((relationship = ANY (ARRAY['self'::text, 'boss'::text, 'coworker'::text, 'other'::text]))),
    CONSTRAINT ck_contact_memberships_scope_space_consistency CHECK (((target_scope <> ALL (ARRAY['personal'::text, 'space'::text])) OR ((target_scope = 'space'::text) AND (target_space_id IS NOT NULL)) OR ((target_scope = 'personal'::text) AND (target_space_id IS NULL)))),
    CONSTRAINT ck_contact_memberships_target_scope CHECK ((target_scope = ANY (ARRAY['personal'::text, 'space'::text])))
);

--
-- Name: contact_memberships_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_memberships_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_memberships_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_memberships_id_seq OWNED BY public.contact_memberships.id;

--
-- Name: contact_type_costs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_type_costs (
    id integer NOT NULL,
    contact_type character varying NOT NULL,
    provider character varying,
    country_code character varying,
    monthly_cost numeric NOT NULL,
    one_time_cost numeric DEFAULT '0'::numeric NOT NULL,
    effective_from timestamp with time zone DEFAULT now(),
    CONSTRAINT ck_contact_type_cost_type CHECK (((contact_type)::text = ANY ((ARRAY['phone'::character varying, 'email'::character varying, 'whatsapp'::character varying, 'discord'::character varying])::text[])))
);

--
-- Name: contact_type_costs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_type_costs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_type_costs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_type_costs_id_seq OWNED BY public.contact_type_costs.id;

--
-- Name: credit_grant_link_claim; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.credit_grant_link_claim (
    id character varying NOT NULL,
    link_id character varying NOT NULL,
    user_id character varying NOT NULL,
    organization_id integer,
    claimed_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: credit_transaction; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.credit_transaction (
    id bigint NOT NULL,
    billing_account_id integer NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    amount numeric NOT NULL,
    category character varying NOT NULL,
    assistant_id integer,
    user_id character varying,
    organization_id integer,
    description character varying,
    detail jsonb,
    plan_assignment_id bigint
);

--
-- Name: credit_transaction_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.credit_transaction_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: credit_transaction_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.credit_transaction_id_seq OWNED BY public.credit_transaction.id;

--
-- Name: dashboard_token; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dashboard_token (
    token character varying(12) NOT NULL,
    entity_type character varying(20) NOT NULL,
    context_name character varying(500) NOT NULL,
    project_id integer NOT NULL,
    user_id character varying NOT NULL,
    organization_id integer,
    created_at timestamp without time zone DEFAULT now()
);

--
-- Name: decommissioned_routes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.decommissioned_routes (
    id integer NOT NULL,
    platform character varying NOT NULL,
    pool_number_id integer NOT NULL,
    contact_identifier character varying NOT NULL,
    old_assistant_id integer NOT NULL,
    new_pool_number_id integer,
    decommissioned_at timestamp with time zone DEFAULT now()
);

--
-- Name: decommissioned_routes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.decommissioned_routes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: decommissioned_routes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.decommissioned_routes_id_seq OWNED BY public.decommissioned_routes.id;

--
-- Name: editor_tile; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.editor_tile (
    id character varying NOT NULL,
    tile_id character varying NOT NULL,
    file_type character varying,
    content character varying,
    file_name character varying
);

--
-- Name: email_account; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.email_account (
    id integer NOT NULL,
    user_id character varying NOT NULL,
    password_hash character varying NOT NULL,
    email_verified boolean DEFAULT false NOT NULL,
    password_changed_at timestamp with time zone,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone
);

--
-- Name: email_account_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.email_account_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: email_account_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.email_account_id_seq OWNED BY public.email_account.id;

--
-- Name: email_verification; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.email_verification (
    id integer NOT NULL,
    email character varying NOT NULL,
    code_hash character varying NOT NULL,
    purpose character varying NOT NULL,
    password_hash character varying,
    name character varying,
    last_name character varying,
    expires_at timestamp with time zone NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    token_jti character varying
);

--
-- Name: email_verification_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.email_verification_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: email_verification_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.email_verification_id_seq OWNED BY public.email_verification.id;

--
-- Name: favorite_project; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.favorite_project (
    id integer NOT NULL,
    user_id character varying NOT NULL,
    project_id integer NOT NULL,
    "position" integer NOT NULL
);

--
-- Name: favorite_project_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.favorite_project_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: favorite_project_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.favorite_project_id_seq OWNED BY public.favorite_project.id;

--
-- Name: interface; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.interface (
    id character varying NOT NULL,
    user_id character varying,
    organization_id integer,
    new_counter integer NOT NULL,
    items character varying NOT NULL,
    name character varying NOT NULL,
    project_id integer NOT NULL,
    context character varying,
    created_at timestamp without time zone DEFAULT now(),
    color character varying,
    is_checkpoint boolean DEFAULT false NOT NULL,
    checkpoint_or_active_id character varying,
    updated_at timestamp without time zone,
    active_tab_id character varying,
    icon character varying DEFAULT 'folder'::character varying NOT NULL,
    "order" integer DEFAULT 0 NOT NULL
);

--
-- Name: interface_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.interface_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: interface_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.interface_id_seq OWNED BY public.interface.id;

--
-- Name: mfa_credential; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mfa_credential (
    id integer NOT NULL,
    user_id character varying NOT NULL,
    method_type character varying NOT NULL,
    credential_data bytea NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    confirmed_at timestamp with time zone,
    last_used_at timestamp with time zone
);

--
-- Name: mfa_credential_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mfa_credential_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: mfa_credential_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mfa_credential_id_seq OWNED BY public.mfa_credential.id;

--
-- Name: mfa_recovery; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mfa_recovery (
    id integer NOT NULL,
    user_id character varying NOT NULL,
    code_hash character varying NOT NULL,
    used boolean DEFAULT false NOT NULL,
    used_at timestamp with time zone,
    created_at timestamp without time zone DEFAULT now()
);

--
-- Name: mfa_recovery_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mfa_recovery_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: mfa_recovery_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mfa_recovery_id_seq OWNED BY public.mfa_recovery.id;

--
-- Name: onboarding_status; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.onboarding_status (
    id character varying DEFAULT (gen_random_uuid())::text NOT NULL,
    user_id character varying NOT NULL,
    current_step character varying(50) NOT NULL,
    step_data jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: one_time_credit_grant_link; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.one_time_credit_grant_link (
    id character varying NOT NULL,
    token character varying NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    credit_amount double precision NOT NULL,
    max_claims integer DEFAULT 1,
    name character varying
);

--
-- Name: organization; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organization (
    id integer NOT NULL,
    owner_id character varying NOT NULL,
    name character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    timezone character varying,
    monthly_spending_cap numeric,
    monthly_spending_cap_set_at timestamp with time zone,
    verified boolean DEFAULT false NOT NULL,
    verified_at timestamp with time zone,
    billing_account_id integer,
    require_mfa boolean DEFAULT false NOT NULL,
    image character varying,
    free_trial boolean DEFAULT false NOT NULL
);

--
-- Name: organization_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.organization_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: organization_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.organization_id_seq OWNED BY public.organization.id;

--
-- Name: organization_invite; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organization_invite (
    id character varying NOT NULL,
    token character varying NOT NULL,
    organization_id integer NOT NULL,
    invitee_email character varying NOT NULL,
    invitee_user_id character varying,
    invited_by_user_id character varying NOT NULL,
    role_id integer NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: organization_member; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organization_member (
    id integer NOT NULL,
    organization_id integer NOT NULL,
    user_id character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    role_id integer NOT NULL,
    monthly_spending_cap numeric,
    monthly_spending_cap_set_at timestamp with time zone
);

--
-- Name: organization_member_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.organization_member_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: organization_member_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.organization_member_id_seq OWNED BY public.organization_member.id;

--
-- Name: permission; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.permission (
    id integer NOT NULL,
    name character varying NOT NULL,
    description character varying,
    resource_type character varying NOT NULL,
    action character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now()
);

--
-- Name: permission_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.permission_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: permission_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.permission_id_seq OWNED BY public.permission.id;

--
-- Name: phone_verifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.phone_verifications (
    id integer NOT NULL,
    user_id character varying NOT NULL,
    phone_number character varying NOT NULL,
    phone_type character varying NOT NULL,
    code_hash character varying NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    verified_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT ck_phone_verifications_type CHECK (((phone_type)::text = ANY ((ARRAY['phone'::character varying, 'whatsapp'::character varying])::text[])))
);

--
-- Name: phone_verifications_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.phone_verifications_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: phone_verifications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.phone_verifications_id_seq OWNED BY public.phone_verifications.id;

--
-- Name: plan_group; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plan_group (
    id bigint NOT NULL,
    name character varying NOT NULL,
    display_name character varying,
    description text,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by_user_id character varying
);

--
-- Name: plan_group_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.plan_group_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: plan_group_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.plan_group_id_seq OWNED BY public.plan_group.id;

--
-- Name: plan_group_member; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plan_group_member (
    group_id bigint NOT NULL,
    template_id bigint NOT NULL,
    "position" integer,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_plan_group_member_position_non_negative CHECK ((("position" IS NULL) OR ("position" >= 0)))
);

--
-- Name: plot; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plot (
    id integer NOT NULL,
    token character varying(12) NOT NULL,
    project_id integer NOT NULL,
    user_id character varying NOT NULL,
    organization_id integer,
    title character varying,
    plot_config jsonb NOT NULL,
    project_config jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now()
);

--
-- Name: plot_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.plot_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: plot_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.plot_id_seq OWNED BY public.plot.id;

--
-- Name: plot_tile; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plot_tile (
    id character varying NOT NULL,
    tile_id character varying NOT NULL,
    plot_type character varying,
    plot_scale_x character varying,
    plot_scale_y character varying,
    plot_aggregate character varying,
    x_axis character varying,
    y_axis character varying,
    plot_group_by character varying,
    plot_group_by_colors character varying,
    bin_count character varying,
    regression_line character varying
);

--
-- Name: rate_limit_counter; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rate_limit_counter (
    id integer NOT NULL,
    user_id character varying NOT NULL,
    organization_id integer,
    endpoint_category character varying(50) NOT NULL,
    endpoint_path character varying(200),
    time_bucket timestamp with time zone NOT NULL,
    request_count integer DEFAULT 1 NOT NULL,
    CONSTRAINT ck_rate_limit_counter_category CHECK (((endpoint_category)::text = ANY ((ARRAY['assistant_hiring'::character varying, 'assistant_media'::character varying, 'assistant_crud'::character varying, 'assistant_voice'::character varying])::text[])))
);

--
-- Name: rate_limit_counter_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.rate_limit_counter_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: rate_limit_counter_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.rate_limit_counter_id_seq OWNED BY public.rate_limit_counter.id;

--
-- Name: recharge; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.recharge (
    id integer NOT NULL,
    at timestamp without time zone NOT NULL,
    quantity numeric NOT NULL,
    type character varying,
    transaction_id character varying,
    status character varying DEFAULT 'pending'::character varying NOT NULL,
    amount_usd numeric DEFAULT 0.00 NOT NULL,
    stripe_invoice_id character varying,
    invoice_group date,
    billing_account_id integer NOT NULL,
    plan_id bigint,
    detail jsonb
);

--
-- Name: recharge_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.recharge_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: recharge_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.recharge_id_seq OWNED BY public.recharge.id;

--
-- Name: recharge_type; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.recharge_type (
    type character varying NOT NULL
);

--
-- Name: resource_access; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_access (
    id integer NOT NULL,
    resource_type character varying NOT NULL,
    resource_id integer NOT NULL,
    role_id integer NOT NULL,
    grantee_type character varying NOT NULL,
    grantee_id character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now()
);

--
-- Name: resource_access_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.resource_access_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: resource_access_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.resource_access_id_seq OWNED BY public.resource_access.id;

--
-- Name: role; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.role (
    id integer NOT NULL,
    name character varying NOT NULL,
    description character varying,
    organization_id integer,
    is_system_role boolean DEFAULT false NOT NULL,
    created_at timestamp without time zone DEFAULT now()
);

--
-- Name: role_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.role_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: role_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.role_id_seq OWNED BY public.role.id;

--
-- Name: role_permission; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.role_permission (
    id integer NOT NULL,
    role_id integer NOT NULL,
    permission_id integer NOT NULL,
    created_at timestamp without time zone DEFAULT now()
);

--
-- Name: role_permission_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.role_permission_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: role_permission_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.role_permission_id_seq OWNED BY public.role_permission.id;

--
-- Name: shared_platform_routes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shared_platform_routes (
    id integer NOT NULL,
    pool_number_id integer NOT NULL,
    contact_number character varying NOT NULL,
    assistant_id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    last_inbound_at timestamp with time zone,
    call_permission_status character varying,
    call_permission_granted_at timestamp with time zone,
    call_permission_expires_at timestamp with time zone
);

--
-- Name: shared_pool_numbers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shared_pool_numbers (
    id integer NOT NULL,
    number character varying NOT NULL,
    status character varying DEFAULT 'active'::character varying NOT NULL,
    twilio_sender_sid character varying,
    created_at timestamp with time zone DEFAULT now(),
    platform character varying DEFAULT 'whatsapp'::character varying NOT NULL,
    auth_token character varying,
    CONSTRAINT ck_shared_pool_number_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'inactive'::character varying])::text[])))
);

--
-- Name: spaces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.spaces (
    space_id bigint NOT NULL,
    name text NOT NULL,
    description text NOT NULL,
    organization_id integer,
    owner_user_id character varying NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    kind text DEFAULT 'team'::text NOT NULL,
    CONSTRAINT ck_spaces_description_length CHECK (((length(description) >= 20) AND (length(description) <= 1000))),
    CONSTRAINT ck_spaces_kind CHECK ((kind = 'team'::text)),
    CONSTRAINT ck_spaces_name_length CHECK (((length(name) >= 1) AND (length(name) <= 200))),
    CONSTRAINT ck_spaces_status CHECK ((status = ANY (ARRAY['active'::text, 'deleting'::text])))
);

--
-- Name: spaces_space_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.spaces_space_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: spaces_space_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.spaces_space_id_seq OWNED BY public.spaces.space_id;

--
-- Name: spending_limit_notifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.spending_limit_notifications (
    id integer NOT NULL,
    entity_type character varying(20) NOT NULL,
    entity_id character varying NOT NULL,
    month character varying(7) NOT NULL,
    limit_value numeric NOT NULL,
    limit_set_at timestamp with time zone,
    notified_at timestamp with time zone DEFAULT now() NOT NULL,
    notified_user_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    entity_name character varying,
    current_spend numeric,
    CONSTRAINT ck_spending_limit_notifications_entity_type CHECK (((entity_type)::text = ANY ((ARRAY['assistant'::character varying, 'user'::character varying, 'member'::character varying, 'organization'::character varying])::text[])))
);

--
-- Name: spending_limit_notifications_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.spending_limit_notifications_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: spending_limit_notifications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.spending_limit_notifications_id_seq OWNED BY public.spending_limit_notifications.id;

--
-- Name: tab; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tab (
    id character varying NOT NULL,
    interface_id character varying NOT NULL,
    name character varying NOT NULL,
    visible boolean DEFAULT true NOT NULL,
    active boolean DEFAULT false NOT NULL,
    "order" integer DEFAULT 0 NOT NULL,
    context character varying,
    color character varying,
    is_checkpoint boolean DEFAULT false NOT NULL,
    checkpoint_or_active_id character varying,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone,
    icon character varying DEFAULT 'tab'::character varying NOT NULL
);

--
-- Name: table_tile; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.table_tile (
    id character varying NOT NULL,
    tile_id character varying NOT NULL,
    table_type character varying,
    page_number character varying,
    column_order character varying,
    hidden_columns character varying,
    sorting character varying,
    group_sorting character varying,
    columns_pin_left character varying,
    columns_pin_right character varying,
    selected character varying,
    default_hidden_columns boolean DEFAULT true NOT NULL
);

--
-- Name: table_view; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.table_view (
    id integer NOT NULL,
    token character varying(12) NOT NULL,
    project_id integer NOT NULL,
    user_id character varying NOT NULL,
    organization_id integer,
    title character varying,
    table_config jsonb NOT NULL,
    project_config jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now()
);

--
-- Name: table_view_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.table_view_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: table_view_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.table_view_id_seq OWNED BY public.table_view.id;

--
-- Name: team; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.team (
    id integer NOT NULL,
    name character varying NOT NULL,
    description character varying,
    organization_id integer NOT NULL,
    created_at timestamp without time zone DEFAULT now()
);

--
-- Name: team_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.team_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: team_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.team_id_seq OWNED BY public.team.id;

--
-- Name: team_member; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.team_member (
    id integer NOT NULL,
    team_id integer NOT NULL,
    user_id character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now()
);

--
-- Name: team_member_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.team_member_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: team_member_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.team_member_id_seq OWNED BY public.team_member.id;

--
-- Name: terminal_tile; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.terminal_tile (
    id character varying NOT NULL,
    tile_id character varying NOT NULL,
    shell_type character varying
);

--
-- Name: tile; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tile (
    id character varying NOT NULL,
    tab_id character varying NOT NULL,
    name character varying NOT NULL,
    type character varying,
    x_position double precision NOT NULL,
    y_position double precision NOT NULL,
    width double precision NOT NULL,
    height double precision NOT NULL,
    visible boolean DEFAULT true NOT NULL,
    locked boolean DEFAULT false NOT NULL,
    moved boolean DEFAULT false NOT NULL,
    static boolean DEFAULT false NOT NULL,
    context character varying,
    "table" character varying,
    auto_update character varying,
    "freeze" character varying,
    filters character varying,
    common_filter character varying,
    metric character varying,
    is_checkpoint boolean DEFAULT false NOT NULL,
    checkpoint_or_active_id character varying,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone,
    column_context character varying,
    "grouping" character varying,
    "minW" double precision,
    "minH" double precision,
    color character varying
);

--
-- Name: user; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."user" (
    id character varying NOT NULL,
    email character varying NOT NULL,
    name character varying,
    last_name character varying,
    job_title character varying,
    tier character varying DEFAULT 'developer'::character varying NOT NULL,
    queries_enabled boolean DEFAULT true NOT NULL,
    evaluations_enabled boolean DEFAULT true NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone,
    image character varying,
    timezone character varying,
    bio character varying,
    phone_number character varying,
    monthly_spending_cap numeric,
    monthly_spending_cap_set_at timestamp with time zone,
    store_prompts boolean DEFAULT true NOT NULL,
    billing_account_id integer,
    whatsapp_number character varying,
    discord_id character varying
);

--
-- Name: user_desktops; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_desktops (
    id integer NOT NULL,
    user_id character varying NOT NULL,
    name character varying NOT NULL,
    url character varying NOT NULL,
    os character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    CONSTRAINT ck_user_desktop_os CHECK (((os)::text = ANY ((ARRAY['ubuntu'::character varying, 'windows'::character varying, 'macos'::character varying])::text[])))
);

--
-- Name: user_desktops_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.user_desktops_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: user_desktops_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.user_desktops_id_seq OWNED BY public.user_desktops.id;

--
-- Name: view_tile; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.view_tile (
    id character varying NOT NULL,
    tile_id character varying NOT NULL,
    base_index character varying
);

--
-- Name: voices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.voices (
    voice_id character varying NOT NULL,
    user_id character varying NOT NULL,
    name character varying NOT NULL,
    description character varying NOT NULL,
    gender character varying,
    language character varying NOT NULL,
    is_preset boolean DEFAULT false NOT NULL,
    provider character varying DEFAULT 'cartesia'::character varying NOT NULL
);

--
-- Name: webhook_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_log (
    id character varying NOT NULL,
    event_id character varying NOT NULL,
    event_type character varying NOT NULL,
    processed_at timestamp without time zone DEFAULT now() NOT NULL
);

--
-- Name: whatsapp_pool_numbers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.whatsapp_pool_numbers_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: whatsapp_pool_numbers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.whatsapp_pool_numbers_id_seq OWNED BY public.shared_pool_numbers.id;

--
-- Name: whatsapp_routes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.whatsapp_routes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: whatsapp_routes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.whatsapp_routes_id_seq OWNED BY public.shared_platform_routes.id;

--
-- Name: admin_user id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_user ALTER COLUMN id SET DEFAULT nextval('public.admin_user_id_seq'::regclass);

--
-- Name: api_key id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_key ALTER COLUMN id SET DEFAULT nextval('public.api_key_id_seq'::regclass);

--
-- Name: assistant_cleanup_tasks id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_cleanup_tasks ALTER COLUMN id SET DEFAULT nextval('public.assistant_cleanup_tasks_id_seq'::regclass);

--
-- Name: assistant_console_config id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_console_config ALTER COLUMN id SET DEFAULT nextval('public.assistant_console_config_id_seq'::regclass);

--
-- Name: assistant_contacts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_contacts ALTER COLUMN id SET DEFAULT nextval('public.assistant_contacts_id_seq'::regclass);

--
-- Name: assistants agent_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistants ALTER COLUMN agent_id SET DEFAULT nextval('public.assistants_agent_id_seq'::regclass);

--
-- Name: auth_rate_limit_entry id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_rate_limit_entry ALTER COLUMN id SET DEFAULT nextval('public.auth_rate_limit_entry_id_seq'::regclass);

--
-- Name: billing_account id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_account ALTER COLUMN id SET DEFAULT nextval('public.billing_account_id_seq'::regclass);

--
-- Name: billing_plan_assignment id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_plan_assignment ALTER COLUMN id SET DEFAULT nextval('public.billing_plan_assignment_id_seq'::regclass);

--
-- Name: billing_plan_template id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_plan_template ALTER COLUMN id SET DEFAULT nextval('public.billing_plan_template_id_seq'::regclass);

--
-- Name: conflict_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conflict_events ALTER COLUMN id SET DEFAULT nextval('public.conflict_events_id_seq'::regclass);

--
-- Name: contact_memberships id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_memberships ALTER COLUMN id SET DEFAULT nextval('public.contact_memberships_id_seq'::regclass);

--
-- Name: contact_type_costs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_type_costs ALTER COLUMN id SET DEFAULT nextval('public.contact_type_costs_id_seq'::regclass);

--
-- Name: credit_transaction id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_transaction ALTER COLUMN id SET DEFAULT nextval('public.credit_transaction_id_seq'::regclass);

--
-- Name: decommissioned_routes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.decommissioned_routes ALTER COLUMN id SET DEFAULT nextval('public.decommissioned_routes_id_seq'::regclass);

--
-- Name: email_account id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_account ALTER COLUMN id SET DEFAULT nextval('public.email_account_id_seq'::regclass);

--
-- Name: email_verification id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_verification ALTER COLUMN id SET DEFAULT nextval('public.email_verification_id_seq'::regclass);

--
-- Name: favorite_project id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.favorite_project ALTER COLUMN id SET DEFAULT nextval('public.favorite_project_id_seq'::regclass);

--
-- Name: interface id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interface ALTER COLUMN id SET DEFAULT nextval('public.interface_id_seq'::regclass);

--
-- Name: mfa_credential id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mfa_credential ALTER COLUMN id SET DEFAULT nextval('public.mfa_credential_id_seq'::regclass);

--
-- Name: mfa_recovery id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mfa_recovery ALTER COLUMN id SET DEFAULT nextval('public.mfa_recovery_id_seq'::regclass);

--
-- Name: organization id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization ALTER COLUMN id SET DEFAULT nextval('public.organization_id_seq'::regclass);

--
-- Name: organization_member id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization_member ALTER COLUMN id SET DEFAULT nextval('public.organization_member_id_seq'::regclass);

--
-- Name: permission id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.permission ALTER COLUMN id SET DEFAULT nextval('public.permission_id_seq'::regclass);

--
-- Name: phone_verifications id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_verifications ALTER COLUMN id SET DEFAULT nextval('public.phone_verifications_id_seq'::regclass);

--
-- Name: plan_group id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_group ALTER COLUMN id SET DEFAULT nextval('public.plan_group_id_seq'::regclass);

--
-- Name: plot id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plot ALTER COLUMN id SET DEFAULT nextval('public.plot_id_seq'::regclass);

--
-- Name: rate_limit_counter id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rate_limit_counter ALTER COLUMN id SET DEFAULT nextval('public.rate_limit_counter_id_seq'::regclass);

--
-- Name: recharge id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recharge ALTER COLUMN id SET DEFAULT nextval('public.recharge_id_seq'::regclass);

--
-- Name: resource_access id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_access ALTER COLUMN id SET DEFAULT nextval('public.resource_access_id_seq'::regclass);

--
-- Name: role id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role ALTER COLUMN id SET DEFAULT nextval('public.role_id_seq'::regclass);

--
-- Name: role_permission id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permission ALTER COLUMN id SET DEFAULT nextval('public.role_permission_id_seq'::regclass);

--
-- Name: shared_platform_routes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shared_platform_routes ALTER COLUMN id SET DEFAULT nextval('public.whatsapp_routes_id_seq'::regclass);

--
-- Name: shared_pool_numbers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shared_pool_numbers ALTER COLUMN id SET DEFAULT nextval('public.whatsapp_pool_numbers_id_seq'::regclass);

--
-- Name: spaces space_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spaces ALTER COLUMN space_id SET DEFAULT nextval('public.spaces_space_id_seq'::regclass);

--
-- Name: spending_limit_notifications id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spending_limit_notifications ALTER COLUMN id SET DEFAULT nextval('public.spending_limit_notifications_id_seq'::regclass);

--
-- Name: table_view id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.table_view ALTER COLUMN id SET DEFAULT nextval('public.table_view_id_seq'::regclass);

--
-- Name: team id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team ALTER COLUMN id SET DEFAULT nextval('public.team_id_seq'::regclass);

--
-- Name: team_member id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_member ALTER COLUMN id SET DEFAULT nextval('public.team_member_id_seq'::regclass);

--
-- Name: user_desktops id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_desktops ALTER COLUMN id SET DEFAULT nextval('public.user_desktops_id_seq'::regclass);

--
-- Name: account account_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account
    ADD CONSTRAINT account_pkey PRIMARY KEY (id);

--
-- Name: admin_user admin_user_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_user
    ADD CONSTRAINT admin_user_pkey PRIMARY KEY (id);

--
-- Name: admin_user admin_user_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_user
    ADD CONSTRAINT admin_user_user_id_key UNIQUE (user_id);

--
-- Name: api_key api_key_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_key
    ADD CONSTRAINT api_key_key_key UNIQUE (key);

--
-- Name: api_key api_key_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_key
    ADD CONSTRAINT api_key_pkey PRIMARY KEY (id);

--
-- Name: api_key api_key_user_id_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_key
    ADD CONSTRAINT api_key_user_id_name_key UNIQUE (user_id, name);

--
-- Name: api_messages api_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_messages
    ADD CONSTRAINT api_messages_pkey PRIMARY KEY (id);

--
-- Name: assistant_cleanup_tasks assistant_cleanup_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_cleanup_tasks
    ADD CONSTRAINT assistant_cleanup_tasks_pkey PRIMARY KEY (id);

--
-- Name: assistant_console_config assistant_console_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_console_config
    ADD CONSTRAINT assistant_console_config_pkey PRIMARY KEY (id);

--
-- Name: assistant_contacts assistant_contacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_contacts
    ADD CONSTRAINT assistant_contacts_pkey PRIMARY KEY (id);

--
-- Name: one_time_credit_grant_link assistant_hiring_one_time_approval_link_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.one_time_credit_grant_link
    ADD CONSTRAINT assistant_hiring_one_time_approval_link_pkey PRIMARY KEY (id);

--
-- Name: assistant_secrets assistant_secrets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_secrets
    ADD CONSTRAINT assistant_secrets_pkey PRIMARY KEY (agent_id, secret_name);

--
-- Name: assistant_space_memberships assistant_space_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_space_memberships
    ADD CONSTRAINT assistant_space_memberships_pkey PRIMARY KEY (assistant_id, space_id);

--
-- Name: assistants assistants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistants
    ADD CONSTRAINT assistants_pkey PRIMARY KEY (agent_id);

--
-- Name: auth_rate_limit_entry auth_rate_limit_entry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_rate_limit_entry
    ADD CONSTRAINT auth_rate_limit_entry_pkey PRIMARY KEY (id);

--
-- Name: billing_account billing_account_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_account
    ADD CONSTRAINT billing_account_pkey PRIMARY KEY (id);

--
-- Name: billing_plan_assignment billing_plan_assignment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_plan_assignment
    ADD CONSTRAINT billing_plan_assignment_pkey PRIMARY KEY (id);

--
-- Name: billing_plan_template billing_plan_template_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_plan_template
    ADD CONSTRAINT billing_plan_template_pkey PRIMARY KEY (id);

--
-- Name: conflict_events conflict_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conflict_events
    ADD CONSTRAINT conflict_events_pkey PRIMARY KEY (id);

--
-- Name: contact_memberships contact_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_memberships
    ADD CONSTRAINT contact_memberships_pkey PRIMARY KEY (id);

--
-- Name: contact_type_costs contact_type_costs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_type_costs
    ADD CONSTRAINT contact_type_costs_pkey PRIMARY KEY (id);

--
-- Name: credit_grant_link_claim credit_grant_link_claim_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_grant_link_claim
    ADD CONSTRAINT credit_grant_link_claim_pkey PRIMARY KEY (id);

--
-- Name: credit_transaction credit_transaction_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_transaction
    ADD CONSTRAINT credit_transaction_pkey PRIMARY KEY (id);

--
-- Name: dashboard_token dashboard_token_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_token
    ADD CONSTRAINT dashboard_token_pkey PRIMARY KEY (token);

--
-- Name: decommissioned_routes decommissioned_routes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.decommissioned_routes
    ADD CONSTRAINT decommissioned_routes_pkey PRIMARY KEY (id);

--
-- Name: editor_tile editor_tile_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.editor_tile
    ADD CONSTRAINT editor_tile_pkey PRIMARY KEY (id);

--
-- Name: email_account email_account_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_account
    ADD CONSTRAINT email_account_pkey PRIMARY KEY (id);

--
-- Name: email_account email_account_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_account
    ADD CONSTRAINT email_account_user_id_key UNIQUE (user_id);

--
-- Name: email_verification email_verification_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_verification
    ADD CONSTRAINT email_verification_pkey PRIMARY KEY (id);

--
-- Name: favorite_project favorite_project_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.favorite_project
    ADD CONSTRAINT favorite_project_pkey PRIMARY KEY (id);

--
-- Name: interface interface_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interface
    ADD CONSTRAINT interface_pkey PRIMARY KEY (id);

--
-- Name: interface it_uq_project_name_checkpoint; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interface
    ADD CONSTRAINT it_uq_project_name_checkpoint UNIQUE (user_id, project_id, name, is_checkpoint);

--
-- Name: mfa_credential mfa_credential_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mfa_credential
    ADD CONSTRAINT mfa_credential_pkey PRIMARY KEY (id);

--
-- Name: mfa_recovery mfa_recovery_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mfa_recovery
    ADD CONSTRAINT mfa_recovery_pkey PRIMARY KEY (id);

--
-- Name: onboarding_status onboarding_status_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.onboarding_status
    ADD CONSTRAINT onboarding_status_pkey PRIMARY KEY (id);

--
-- Name: organization_invite organization_invite_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization_invite
    ADD CONSTRAINT organization_invite_pkey PRIMARY KEY (id);

--
-- Name: organization_member organization_member_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization_member
    ADD CONSTRAINT organization_member_pkey PRIMARY KEY (id);

--
-- Name: organization organization_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization
    ADD CONSTRAINT organization_name_key UNIQUE (name);

--
-- Name: organization organization_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization
    ADD CONSTRAINT organization_pkey PRIMARY KEY (id);

--
-- Name: permission permission_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.permission
    ADD CONSTRAINT permission_name_key UNIQUE (name);

--
-- Name: permission permission_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.permission
    ADD CONSTRAINT permission_pkey PRIMARY KEY (id);

--
-- Name: phone_verifications phone_verifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_verifications
    ADD CONSTRAINT phone_verifications_pkey PRIMARY KEY (id);

--
-- Name: plan_group_member plan_group_member_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_group_member
    ADD CONSTRAINT plan_group_member_pkey PRIMARY KEY (group_id, template_id);

--
-- Name: plan_group plan_group_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_group
    ADD CONSTRAINT plan_group_pkey PRIMARY KEY (id);

--
-- Name: plot plot_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plot
    ADD CONSTRAINT plot_pkey PRIMARY KEY (id);

--
-- Name: plot_tile plot_tile_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plot_tile
    ADD CONSTRAINT plot_tile_pkey PRIMARY KEY (id);

--
-- Name: rate_limit_counter rate_limit_counter_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rate_limit_counter
    ADD CONSTRAINT rate_limit_counter_pkey PRIMARY KEY (id);

--
-- Name: recharge recharge_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recharge
    ADD CONSTRAINT recharge_pkey PRIMARY KEY (id);

--
-- Name: recharge_type recharge_type_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recharge_type
    ADD CONSTRAINT recharge_type_pkey PRIMARY KEY (type);

--
-- Name: resource_access resource_access_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_access
    ADD CONSTRAINT resource_access_pkey PRIMARY KEY (id);

--
-- Name: role_permission role_permission_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permission
    ADD CONSTRAINT role_permission_pkey PRIMARY KEY (id);

--
-- Name: role role_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role
    ADD CONSTRAINT role_pkey PRIMARY KEY (id);

--
-- Name: spaces spaces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spaces
    ADD CONSTRAINT spaces_pkey PRIMARY KEY (space_id);

--
-- Name: spending_limit_notifications spending_limit_notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spending_limit_notifications
    ADD CONSTRAINT spending_limit_notifications_pkey PRIMARY KEY (id);

--
-- Name: tab tab_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tab
    ADD CONSTRAINT tab_pkey PRIMARY KEY (id);

--
-- Name: tab tab_uq_interface_name_checkpoint; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tab
    ADD CONSTRAINT tab_uq_interface_name_checkpoint UNIQUE (interface_id, name, is_checkpoint);

--
-- Name: table_tile table_tile_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.table_tile
    ADD CONSTRAINT table_tile_pkey PRIMARY KEY (id);

--
-- Name: table_view table_view_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.table_view
    ADD CONSTRAINT table_view_pkey PRIMARY KEY (id);

--
-- Name: team_member team_member_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_member
    ADD CONSTRAINT team_member_pkey PRIMARY KEY (id);

--
-- Name: team team_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team
    ADD CONSTRAINT team_pkey PRIMARY KEY (id);

--
-- Name: terminal_tile terminal_tile_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.terminal_tile
    ADD CONSTRAINT terminal_tile_pkey PRIMARY KEY (id);

--
-- Name: tile tile_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tile
    ADD CONSTRAINT tile_pkey PRIMARY KEY (id);

--
-- Name: tile tile_uq_tab_name_checkpoint; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tile
    ADD CONSTRAINT tile_uq_tab_name_checkpoint UNIQUE (tab_id, name, is_checkpoint);

--
-- Name: assistants uq_assistant_user_desktop_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistants
    ADD CONSTRAINT uq_assistant_user_desktop_id UNIQUE (user_desktop_id);

--
-- Name: auth_rate_limit_entry uq_auth_rate_limit_entry; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_rate_limit_entry
    ADD CONSTRAINT uq_auth_rate_limit_entry UNIQUE (key, endpoint_category, time_bucket);

--
-- Name: credit_grant_link_claim uq_claim_link_user; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_grant_link_claim
    ADD CONSTRAINT uq_claim_link_user UNIQUE (link_id, user_id);

--
-- Name: contact_type_costs uq_contact_cost; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_type_costs
    ADD CONSTRAINT uq_contact_cost UNIQUE (contact_type, provider, country_code);

--
-- Name: plot uq_plot_token; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plot
    ADD CONSTRAINT uq_plot_token UNIQUE (token);

--
-- Name: shared_platform_routes uq_pool_contact; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shared_platform_routes
    ADD CONSTRAINT uq_pool_contact UNIQUE (pool_number_id, contact_number);

--
-- Name: rate_limit_counter uq_rate_limit_counter; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rate_limit_counter
    ADD CONSTRAINT uq_rate_limit_counter UNIQUE (user_id, endpoint_category, endpoint_path, time_bucket);

--
-- Name: resource_access uq_resource_access_grantee; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_access
    ADD CONSTRAINT uq_resource_access_grantee UNIQUE (resource_type, resource_id, grantee_type, grantee_id);

--
-- Name: role uq_role_name_org; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role
    ADD CONSTRAINT uq_role_name_org UNIQUE (name, organization_id);

--
-- Name: role_permission uq_role_permission; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permission
    ADD CONSTRAINT uq_role_permission UNIQUE (role_id, permission_id);

--
-- Name: team_member uq_team_member; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_member
    ADD CONSTRAINT uq_team_member UNIQUE (team_id, user_id);

--
-- Name: team uq_team_name_org; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team
    ADD CONSTRAINT uq_team_name_org UNIQUE (name, organization_id);

--
-- Name: favorite_project uq_user_favorite_project; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.favorite_project
    ADD CONSTRAINT uq_user_favorite_project UNIQUE (user_id, project_id);

--
-- Name: user_desktops user_desktops_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_desktops
    ADD CONSTRAINT user_desktops_pkey PRIMARY KEY (id);

--
-- Name: user user_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."user"
    ADD CONSTRAINT user_pkey PRIMARY KEY (id);

--
-- Name: billing_plan_template ux_billing_plan_template_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_plan_template
    ADD CONSTRAINT ux_billing_plan_template_name UNIQUE (name);

--
-- Name: plan_group ux_plan_group_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_group
    ADD CONSTRAINT ux_plan_group_name UNIQUE (name);

--
-- Name: view_tile view_tile_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.view_tile
    ADD CONSTRAINT view_tile_pkey PRIMARY KEY (id);

--
-- Name: voices voices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.voices
    ADD CONSTRAINT voices_pkey PRIMARY KEY (user_id, voice_id, provider);

--
-- Name: webhook_log webhook_log_event_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_log
    ADD CONSTRAINT webhook_log_event_id_key UNIQUE (event_id);

--
-- Name: webhook_log webhook_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_log
    ADD CONSTRAINT webhook_log_pkey PRIMARY KEY (id);

--
-- Name: shared_pool_numbers whatsapp_pool_numbers_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shared_pool_numbers
    ADD CONSTRAINT whatsapp_pool_numbers_number_key UNIQUE (number);

--
-- Name: shared_pool_numbers whatsapp_pool_numbers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shared_pool_numbers
    ADD CONSTRAINT whatsapp_pool_numbers_pkey PRIMARY KEY (id);

--
-- Name: shared_platform_routes whatsapp_routes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shared_platform_routes
    ADD CONSTRAINT whatsapp_routes_pkey PRIMARY KEY (id);

--
-- Name: idx_dashboard_token_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dashboard_token_project_id ON public.dashboard_token USING btree (project_id);

--
-- Name: idx_dashboard_token_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dashboard_token_user_id ON public.dashboard_token USING btree (user_id);

--
-- Name: idx_plot_organization_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plot_organization_id ON public.plot USING btree (organization_id);

--
-- Name: idx_plot_project_config_context; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plot_project_config_context ON public.plot USING btree (((project_config ->> 'context'::text)));

--
-- Name: idx_plot_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plot_project_id ON public.plot USING btree (project_id);

--
-- Name: idx_plot_token; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_plot_token ON public.plot USING btree (token);

--
-- Name: idx_plot_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plot_user_id ON public.plot USING btree (user_id);

--
-- Name: idx_recharge_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_recharge_pending ON public.recharge USING btree (status, invoice_group);

--
-- Name: idx_resource_access_grantee; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resource_access_grantee ON public.resource_access USING btree (grantee_type, grantee_id);

--
-- Name: idx_resource_access_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resource_access_resource ON public.resource_access USING btree (resource_type, resource_id);

--
-- Name: idx_table_view_organization_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_table_view_organization_id ON public.table_view USING btree (organization_id);

--
-- Name: idx_table_view_project_config_context; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_table_view_project_config_context ON public.table_view USING btree (((project_config ->> 'context'::text)));

--
-- Name: idx_table_view_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_table_view_project_id ON public.table_view USING btree (project_id);

--
-- Name: idx_table_view_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_table_view_user_id ON public.table_view USING btree (user_id);

--
-- Name: ix_api_messages_assistant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_messages_assistant_id ON public.api_messages USING btree (assistant_id);

--
-- Name: ix_api_messages_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_messages_user_id ON public.api_messages USING btree (user_id);

--
-- Name: ix_asm_space_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asm_space_id ON public.assistant_space_memberships USING btree (space_id);

--
-- Name: ix_assistant_cleanup_tasks_assistant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assistant_cleanup_tasks_assistant ON public.assistant_cleanup_tasks USING btree (assistant_id);

--
-- Name: ix_assistant_cleanup_tasks_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assistant_cleanup_tasks_status ON public.assistant_cleanup_tasks USING btree (status, next_retry_at);

--
-- Name: ix_assistant_console_config_assistant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_assistant_console_config_assistant_id ON public.assistant_console_config USING btree (assistant_id);

--
-- Name: ix_assistant_contacts_assistant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assistant_contacts_assistant_id ON public.assistant_contacts USING btree (assistant_id);

--
-- Name: ix_assistant_hiring_one_time_approval_link_token; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_assistant_hiring_one_time_approval_link_token ON public.one_time_credit_grant_link USING btree (token);

--
-- Name: ix_assistant_secrets_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assistant_secrets_agent_id ON public.assistant_secrets USING btree (agent_id);

--
-- Name: ix_assistant_secrets_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assistant_secrets_user_id ON public.assistant_secrets USING btree (user_id);

--
-- Name: ix_assistants_last_correspondence_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assistants_last_correspondence_at ON public.assistants USING btree (last_correspondence_at);

--
-- Name: ix_assistants_organization_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assistants_organization_id ON public.assistants USING btree (organization_id);

--
-- Name: ix_assistants_termination_initiated_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assistants_termination_initiated_at ON public.assistants USING btree (termination_initiated_at);

--
-- Name: ix_assistants_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assistants_user_id ON public.assistants USING btree (user_id);

--
-- Name: ix_assistants_voice_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assistants_voice_id ON public.assistants USING btree (voice_id);

--
-- Name: ix_auth_rate_limit_entry_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_auth_rate_limit_entry_key ON public.auth_rate_limit_entry USING btree (key);

--
-- Name: ix_auth_rate_limit_key_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_auth_rate_limit_key_category ON public.auth_rate_limit_entry USING btree (key, endpoint_category, time_bucket);

--
-- Name: ix_auth_rate_limit_time_bucket; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_auth_rate_limit_time_bucket ON public.auth_rate_limit_entry USING btree (time_bucket);

--
-- Name: ix_billing_account_plan_assignment_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_billing_account_plan_assignment_id ON public.billing_account USING btree (plan_assignment_id);

--
-- Name: ix_billing_account_plan_group_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_billing_account_plan_group_id ON public.billing_account USING btree (plan_group_id);

--
-- Name: ix_billing_account_stripe_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_billing_account_stripe_customer_id ON public.billing_account USING btree (stripe_customer_id);

--
-- Name: ix_billing_plan_assignment_account_started; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_billing_plan_assignment_account_started ON public.billing_plan_assignment USING btree (billing_account_id, started_at);

--
-- Name: ix_billing_plan_assignment_billing_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_billing_plan_assignment_billing_account_id ON public.billing_plan_assignment USING btree (billing_account_id);

--
-- Name: ix_billing_plan_assignment_template_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_billing_plan_assignment_template_id ON public.billing_plan_assignment USING btree (template_id);

--
-- Name: ix_conflict_events_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_conflict_events_status ON public.conflict_events USING btree (status);

--
-- Name: ix_conflict_events_trigger_assistant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_conflict_events_trigger_assistant ON public.conflict_events USING btree (trigger_assistant_id);

--
-- Name: ix_contact_memberships_assistant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_contact_memberships_assistant_id ON public.contact_memberships USING btree (assistant_id);

--
-- Name: ix_contact_memberships_assistant_personal_self; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_contact_memberships_assistant_personal_self ON public.contact_memberships USING btree (assistant_id) WHERE ((target_scope = 'personal'::text) AND (relationship = 'self'::text));

--
-- Name: ix_contact_memberships_assistant_space_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_contact_memberships_assistant_space_target ON public.contact_memberships USING btree (assistant_id, target_space_id) WHERE (target_scope = 'space'::text);

--
-- Name: ix_contact_memberships_authoring_assistant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_contact_memberships_authoring_assistant_id ON public.contact_memberships USING btree (authoring_assistant_id) WHERE (authoring_assistant_id IS NOT NULL);

--
-- Name: ix_contact_memberships_target_space_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_contact_memberships_target_space_id ON public.contact_memberships USING btree (target_space_id) WHERE (target_space_id IS NOT NULL);

--
-- Name: ix_credit_grant_link_claim_link_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_credit_grant_link_claim_link_id ON public.credit_grant_link_claim USING btree (link_id);

--
-- Name: ix_credit_grant_link_claim_organization_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_credit_grant_link_claim_organization_id ON public.credit_grant_link_claim USING btree (organization_id);

--
-- Name: ix_credit_grant_link_claim_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_credit_grant_link_claim_user_id ON public.credit_grant_link_claim USING btree (user_id);

--
-- Name: ix_credit_txn_assistant_category_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_credit_txn_assistant_category_at ON public.credit_transaction USING btree (assistant_id, category, at);

--
-- Name: ix_credit_txn_ba_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_credit_txn_ba_at ON public.credit_transaction USING btree (billing_account_id, at);

--
-- Name: ix_credit_txn_ba_category_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_credit_txn_ba_category_at ON public.credit_transaction USING btree (billing_account_id, category, at);

--
-- Name: ix_credit_txn_plan_assignment; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_credit_txn_plan_assignment ON public.credit_transaction USING btree (plan_assignment_id);

--
-- Name: ix_credit_txn_user_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_credit_txn_user_at ON public.credit_transaction USING btree (user_id, at);

--
-- Name: ix_decommissioned_routes_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_decommissioned_routes_lookup ON public.decommissioned_routes USING btree (pool_number_id, contact_identifier);

--
-- Name: ix_editor_tile_tile_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_editor_tile_tile_id ON public.editor_tile USING btree (tile_id);

--
-- Name: ix_email_verification_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_email_verification_email ON public.email_verification USING btree (email);

--
-- Name: ix_favorite_project_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_favorite_project_user_id ON public.favorite_project USING btree (user_id);

--
-- Name: ix_interface_organization_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_interface_organization_id ON public.interface USING btree (organization_id);

--
-- Name: ix_interface_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_interface_project_id ON public.interface USING btree (project_id);

--
-- Name: ix_interface_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_interface_user_id ON public.interface USING btree (user_id);

--
-- Name: ix_mfa_credential_user_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mfa_credential_user_type ON public.mfa_credential USING btree (user_id, method_type);

--
-- Name: ix_mfa_recovery_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mfa_recovery_user_id ON public.mfa_recovery USING btree (user_id);

--
-- Name: ix_onboarding_status_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_onboarding_status_user_id ON public.onboarding_status USING btree (user_id);

--
-- Name: ix_organization_billing_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_organization_billing_account_id ON public.organization USING btree (billing_account_id);

--
-- Name: ix_organization_invite_invitee_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_organization_invite_invitee_email ON public.organization_invite USING btree (invitee_email);

--
-- Name: ix_organization_invite_organization_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_organization_invite_organization_id ON public.organization_invite USING btree (organization_id);

--
-- Name: ix_organization_invite_token; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_organization_invite_token ON public.organization_invite USING btree (token);

--
-- Name: ix_organization_verified; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_organization_verified ON public.organization USING btree (verified);

--
-- Name: ix_phone_verifications_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_phone_verifications_user_id ON public.phone_verifications USING btree (user_id);

--
-- Name: ix_plan_group_is_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_plan_group_is_active ON public.plan_group USING btree (is_active);

--
-- Name: ix_plan_group_member_template_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_plan_group_member_template_id ON public.plan_group_member USING btree (template_id);

--
-- Name: ix_plan_template_is_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_plan_template_is_active ON public.billing_plan_template USING btree (is_active);

--
-- Name: ix_plot_tile_tile_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_plot_tile_tile_id ON public.plot_tile USING btree (tile_id);

--
-- Name: ix_rate_limit_counter_endpoint; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rate_limit_counter_endpoint ON public.rate_limit_counter USING btree (user_id, endpoint_path, time_bucket);

--
-- Name: ix_rate_limit_counter_org_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rate_limit_counter_org_category ON public.rate_limit_counter USING btree (organization_id, endpoint_category, time_bucket);

--
-- Name: ix_rate_limit_counter_organization_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rate_limit_counter_organization_id ON public.rate_limit_counter USING btree (organization_id);

--
-- Name: ix_rate_limit_counter_time_bucket; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rate_limit_counter_time_bucket ON public.rate_limit_counter USING btree (time_bucket);

--
-- Name: ix_rate_limit_counter_user_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rate_limit_counter_user_category ON public.rate_limit_counter USING btree (user_id, endpoint_category, time_bucket);

--
-- Name: ix_rate_limit_counter_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rate_limit_counter_user_id ON public.rate_limit_counter USING btree (user_id);

--
-- Name: ix_recharge_billing_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recharge_billing_account_id ON public.recharge USING btree (billing_account_id);

--
-- Name: ix_recharge_plan_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recharge_plan_id ON public.recharge USING btree (plan_id);

--
-- Name: ix_shared_routes_assistant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_shared_routes_assistant ON public.shared_platform_routes USING btree (assistant_id, contact_number);

--
-- Name: ix_shared_routes_contact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_shared_routes_contact ON public.shared_platform_routes USING btree (contact_number);

--
-- Name: ix_spaces_organization_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_spaces_organization_id ON public.spaces USING btree (organization_id);

--
-- Name: ix_spaces_owner_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_spaces_owner_user_id ON public.spaces USING btree (owner_user_id);

--
-- Name: ix_spending_limit_notifications_dedupe; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_spending_limit_notifications_dedupe ON public.spending_limit_notifications USING btree (entity_type, entity_id, month, limit_value);

--
-- Name: ix_spending_limit_notifications_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_spending_limit_notifications_entity ON public.spending_limit_notifications USING btree (entity_type, entity_id);

--
-- Name: ix_spending_limit_notifications_month; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_spending_limit_notifications_month ON public.spending_limit_notifications USING btree (month);

--
-- Name: ix_tab_interface_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tab_interface_id ON public.tab USING btree (interface_id);

--
-- Name: ix_table_tile_tile_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_table_tile_tile_id ON public.table_tile USING btree (tile_id);

--
-- Name: ix_table_view_token; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_table_view_token ON public.table_view USING btree (token);

--
-- Name: ix_terminal_tile_tile_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_terminal_tile_tile_id ON public.terminal_tile USING btree (tile_id);

--
-- Name: ix_tile_tab_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tile_tab_id ON public.tile USING btree (tab_id);

--
-- Name: ix_user_billing_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_billing_account_id ON public."user" USING btree (billing_account_id);

--
-- Name: ix_user_desktops_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_desktops_user_id ON public.user_desktops USING btree (user_id);

--
-- Name: ix_user_email; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_user_email ON public."user" USING btree (email);

--
-- Name: ix_view_tile_tile_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_view_tile_tile_id ON public.view_tile USING btree (tile_id);

--
-- Name: ix_voices_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_voices_user_id ON public.voices USING btree (user_id);

--
-- Name: uq_active_contact_value; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_active_contact_value ON public.assistant_contacts USING btree (contact_value) WHERE (((status)::text <> 'deleted'::text) AND ((contact_type)::text <> ALL ((ARRAY['whatsapp'::character varying, 'discord'::character varying])::text[])));

--
-- Name: uq_assistant_contact_type_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_assistant_contact_type_active ON public.assistant_contacts USING btree (assistant_id, contact_type) WHERE ((status)::text <> 'deleted'::text);

--
-- Name: uq_user_discord_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_user_discord_id ON public."user" USING btree (discord_id) WHERE (discord_id IS NOT NULL);

--
-- Name: uq_user_whatsapp_number; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_user_whatsapp_number ON public."user" USING btree (whatsapp_number) WHERE (whatsapp_number IS NOT NULL);

--
-- Name: ux_assistants_one_personal_coordinator_per_user; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_assistants_one_personal_coordinator_per_user ON public.assistants USING btree (user_id) WHERE (is_coordinator AND (organization_id IS NULL));

--
-- Name: ux_assistants_one_workspace_coordinator_per_membership; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_assistants_one_workspace_coordinator_per_membership ON public.assistants USING btree (user_id, organization_id) WHERE (is_coordinator AND (organization_id IS NOT NULL));

--
-- Name: ux_billing_plan_assignment_active_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_billing_plan_assignment_active_unique ON public.billing_plan_assignment USING btree (billing_account_id) WHERE (ended_at IS NULL);

--
-- Name: ux_contact_memberships_personal_pair; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_contact_memberships_personal_pair ON public.contact_memberships USING btree (assistant_id, contact_id) WHERE (target_scope = 'personal'::text);

--
-- Name: ux_contact_memberships_space_pair; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_contact_memberships_space_pair ON public.contact_memberships USING btree (assistant_id, contact_id, target_space_id) WHERE (target_scope = 'space'::text);

--
-- Name: ux_plan_group_member_position; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_plan_group_member_position ON public.plan_group_member USING btree (group_id, "position") WHERE ("position" IS NOT NULL);

--
-- Name: account account_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account
    ADD CONSTRAINT account_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: admin_user admin_user_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_user
    ADD CONSTRAINT admin_user_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: api_key api_key_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_key
    ADD CONSTRAINT api_key_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organization(id) ON DELETE CASCADE;

--
-- Name: api_key api_key_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_key
    ADD CONSTRAINT api_key_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: api_messages api_messages_assistant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_messages
    ADD CONSTRAINT api_messages_assistant_id_fkey FOREIGN KEY (assistant_id) REFERENCES public.assistants(agent_id) ON DELETE CASCADE;

--
-- Name: api_messages api_messages_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_messages
    ADD CONSTRAINT api_messages_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: assistant_console_config assistant_console_config_assistant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_console_config
    ADD CONSTRAINT assistant_console_config_assistant_id_fkey FOREIGN KEY (assistant_id) REFERENCES public.assistants(agent_id) ON DELETE CASCADE;

--
-- Name: assistant_contacts assistant_contacts_assistant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_contacts
    ADD CONSTRAINT assistant_contacts_assistant_id_fkey FOREIGN KEY (assistant_id) REFERENCES public.assistants(agent_id) ON DELETE CASCADE;

--
-- Name: assistant_secrets assistant_secrets_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_secrets
    ADD CONSTRAINT assistant_secrets_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.assistants(agent_id) ON DELETE CASCADE;

--
-- Name: assistant_secrets assistant_secrets_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_secrets
    ADD CONSTRAINT assistant_secrets_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: assistant_space_memberships assistant_space_memberships_assistant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_space_memberships
    ADD CONSTRAINT assistant_space_memberships_assistant_id_fkey FOREIGN KEY (assistant_id) REFERENCES public.assistants(agent_id) ON DELETE CASCADE;

--
-- Name: assistant_space_memberships assistant_space_memberships_space_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_space_memberships
    ADD CONSTRAINT assistant_space_memberships_space_id_fkey FOREIGN KEY (space_id) REFERENCES public.spaces(space_id) ON DELETE CASCADE;

--
-- Name: assistants assistants_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistants
    ADD CONSTRAINT assistants_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organization(id) ON DELETE CASCADE;

--
-- Name: assistants assistants_user_desktop_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistants
    ADD CONSTRAINT assistants_user_desktop_id_fkey FOREIGN KEY (user_desktop_id) REFERENCES public.user_desktops(id) ON DELETE SET NULL;

--
-- Name: assistants assistants_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistants
    ADD CONSTRAINT assistants_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: billing_plan_assignment billing_plan_assignment_billing_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_plan_assignment
    ADD CONSTRAINT billing_plan_assignment_billing_account_id_fkey FOREIGN KEY (billing_account_id) REFERENCES public.billing_account(id) ON DELETE CASCADE;

--
-- Name: billing_plan_assignment billing_plan_assignment_created_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_plan_assignment
    ADD CONSTRAINT billing_plan_assignment_created_by_user_id_fkey FOREIGN KEY (created_by_user_id) REFERENCES public."user"(id) ON DELETE SET NULL;

--
-- Name: billing_plan_assignment billing_plan_assignment_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_plan_assignment
    ADD CONSTRAINT billing_plan_assignment_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.billing_plan_template(id) ON DELETE RESTRICT;

--
-- Name: billing_plan_template billing_plan_template_created_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_plan_template
    ADD CONSTRAINT billing_plan_template_created_by_user_id_fkey FOREIGN KEY (created_by_user_id) REFERENCES public."user"(id) ON DELETE SET NULL;

--
-- Name: billing_plan_template billing_plan_template_supersedes_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_plan_template
    ADD CONSTRAINT billing_plan_template_supersedes_template_id_fkey FOREIGN KEY (supersedes_template_id) REFERENCES public.billing_plan_template(id) ON DELETE SET NULL;

--
-- Name: conflict_events conflict_events_trigger_assistant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conflict_events
    ADD CONSTRAINT conflict_events_trigger_assistant_id_fkey FOREIGN KEY (trigger_assistant_id) REFERENCES public.assistants(agent_id) ON DELETE SET NULL;

--
-- Name: contact_memberships contact_memberships_assistant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_memberships
    ADD CONSTRAINT contact_memberships_assistant_id_fkey FOREIGN KEY (assistant_id) REFERENCES public.assistants(agent_id) ON DELETE CASCADE;

--
-- Name: contact_memberships contact_memberships_target_space_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_memberships
    ADD CONSTRAINT contact_memberships_target_space_id_fkey FOREIGN KEY (target_space_id) REFERENCES public.spaces(space_id) ON DELETE CASCADE;

--
-- Name: credit_grant_link_claim credit_grant_link_claim_link_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_grant_link_claim
    ADD CONSTRAINT credit_grant_link_claim_link_id_fkey FOREIGN KEY (link_id) REFERENCES public.one_time_credit_grant_link(id) ON DELETE CASCADE;

--
-- Name: credit_grant_link_claim credit_grant_link_claim_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_grant_link_claim
    ADD CONSTRAINT credit_grant_link_claim_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organization(id);

--
-- Name: credit_grant_link_claim credit_grant_link_claim_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_grant_link_claim
    ADD CONSTRAINT credit_grant_link_claim_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id);

--
-- Name: credit_transaction credit_transaction_billing_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_transaction
    ADD CONSTRAINT credit_transaction_billing_account_id_fkey FOREIGN KEY (billing_account_id) REFERENCES public.billing_account(id) ON DELETE CASCADE;

--
-- Name: dashboard_token dashboard_token_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_token
    ADD CONSTRAINT dashboard_token_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organization(id) ON DELETE CASCADE;

--
-- Name: dashboard_token dashboard_token_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_token
    ADD CONSTRAINT dashboard_token_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.project(id) ON DELETE CASCADE;

--
-- Name: dashboard_token dashboard_token_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_token
    ADD CONSTRAINT dashboard_token_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: decommissioned_routes decommissioned_routes_new_pool_number_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.decommissioned_routes
    ADD CONSTRAINT decommissioned_routes_new_pool_number_id_fkey FOREIGN KEY (new_pool_number_id) REFERENCES public.shared_pool_numbers(id) ON DELETE CASCADE;

--
-- Name: decommissioned_routes decommissioned_routes_old_assistant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.decommissioned_routes
    ADD CONSTRAINT decommissioned_routes_old_assistant_id_fkey FOREIGN KEY (old_assistant_id) REFERENCES public.assistants(agent_id) ON DELETE CASCADE;

--
-- Name: decommissioned_routes decommissioned_routes_pool_number_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.decommissioned_routes
    ADD CONSTRAINT decommissioned_routes_pool_number_id_fkey FOREIGN KEY (pool_number_id) REFERENCES public.shared_pool_numbers(id) ON DELETE CASCADE;

--
-- Name: editor_tile editor_tile_tile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.editor_tile
    ADD CONSTRAINT editor_tile_tile_id_fkey FOREIGN KEY (tile_id) REFERENCES public.tile(id) ON DELETE CASCADE;

--
-- Name: email_account email_account_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_account
    ADD CONSTRAINT email_account_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: favorite_project favorite_project_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.favorite_project
    ADD CONSTRAINT favorite_project_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.project(id) ON DELETE CASCADE;

--
-- Name: favorite_project favorite_project_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.favorite_project
    ADD CONSTRAINT favorite_project_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: assistants fk_assistants_voices; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistants
    ADD CONSTRAINT fk_assistants_voices FOREIGN KEY (user_id, voice_id, voice_provider) REFERENCES public.voices(user_id, voice_id, provider);

--
-- Name: billing_account fk_billing_account_plan_assignment; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_account
    ADD CONSTRAINT fk_billing_account_plan_assignment FOREIGN KEY (plan_assignment_id) REFERENCES public.billing_plan_assignment(id) ON DELETE SET NULL;

--
-- Name: billing_account fk_billing_account_plan_group; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_account
    ADD CONSTRAINT fk_billing_account_plan_group FOREIGN KEY (plan_group_id) REFERENCES public.plan_group(id) ON DELETE RESTRICT;

--
-- Name: contact_memberships fk_contact_memberships_authoring_assistant_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_memberships
    ADD CONSTRAINT fk_contact_memberships_authoring_assistant_id FOREIGN KEY (authoring_assistant_id) REFERENCES public.assistants(agent_id) ON DELETE SET NULL;

--
-- Name: credit_transaction fk_credit_txn_plan_assignment; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_transaction
    ADD CONSTRAINT fk_credit_txn_plan_assignment FOREIGN KEY (plan_assignment_id) REFERENCES public.billing_plan_assignment(id) ON DELETE SET NULL;

--
-- Name: organization fk_organization_billing_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization
    ADD CONSTRAINT fk_organization_billing_account_id FOREIGN KEY (billing_account_id) REFERENCES public.billing_account(id) ON DELETE SET NULL;

--
-- Name: organization_member fk_organization_member_role_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization_member
    ADD CONSTRAINT fk_organization_member_role_id FOREIGN KEY (role_id) REFERENCES public.role(id) ON DELETE RESTRICT;

--
-- Name: recharge fk_recharge_billing_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recharge
    ADD CONSTRAINT fk_recharge_billing_account_id FOREIGN KEY (billing_account_id) REFERENCES public.billing_account(id) ON DELETE CASCADE;

--
-- Name: recharge fk_recharge_plan; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recharge
    ADD CONSTRAINT fk_recharge_plan FOREIGN KEY (plan_id) REFERENCES public.billing_plan_assignment(id) ON DELETE SET NULL;

--
-- Name: user fk_user_billing_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."user"
    ADD CONSTRAINT fk_user_billing_account_id FOREIGN KEY (billing_account_id) REFERENCES public.billing_account(id) ON DELETE SET NULL;

--
-- Name: interface interface_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interface
    ADD CONSTRAINT interface_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organization(id) ON DELETE CASCADE;

--
-- Name: interface interface_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interface
    ADD CONSTRAINT interface_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.project(id) ON DELETE CASCADE;

--
-- Name: interface interface_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interface
    ADD CONSTRAINT interface_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: mfa_credential mfa_credential_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mfa_credential
    ADD CONSTRAINT mfa_credential_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: mfa_recovery mfa_recovery_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mfa_recovery
    ADD CONSTRAINT mfa_recovery_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: onboarding_status onboarding_status_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.onboarding_status
    ADD CONSTRAINT onboarding_status_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: organization_invite organization_invite_invited_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization_invite
    ADD CONSTRAINT organization_invite_invited_by_user_id_fkey FOREIGN KEY (invited_by_user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: organization_invite organization_invite_invitee_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization_invite
    ADD CONSTRAINT organization_invite_invitee_user_id_fkey FOREIGN KEY (invitee_user_id) REFERENCES public."user"(id) ON DELETE SET NULL;

--
-- Name: organization_invite organization_invite_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization_invite
    ADD CONSTRAINT organization_invite_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organization(id) ON DELETE CASCADE;

--
-- Name: organization_invite organization_invite_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization_invite
    ADD CONSTRAINT organization_invite_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.role(id) ON DELETE RESTRICT;

--
-- Name: organization_member organization_member_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization_member
    ADD CONSTRAINT organization_member_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organization(id) ON DELETE CASCADE;

--
-- Name: organization_member organization_member_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization_member
    ADD CONSTRAINT organization_member_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: organization organization_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization
    ADD CONSTRAINT organization_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: phone_verifications phone_verifications_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_verifications
    ADD CONSTRAINT phone_verifications_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: plan_group plan_group_created_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_group
    ADD CONSTRAINT plan_group_created_by_user_id_fkey FOREIGN KEY (created_by_user_id) REFERENCES public."user"(id) ON DELETE SET NULL;

--
-- Name: plan_group_member plan_group_member_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_group_member
    ADD CONSTRAINT plan_group_member_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.plan_group(id) ON DELETE CASCADE;

--
-- Name: plan_group_member plan_group_member_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_group_member
    ADD CONSTRAINT plan_group_member_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.billing_plan_template(id) ON DELETE RESTRICT;

--
-- Name: plot plot_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plot
    ADD CONSTRAINT plot_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organization(id) ON DELETE CASCADE;

--
-- Name: plot plot_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plot
    ADD CONSTRAINT plot_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.project(id) ON DELETE CASCADE;

--
-- Name: plot_tile plot_tile_tile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plot_tile
    ADD CONSTRAINT plot_tile_tile_id_fkey FOREIGN KEY (tile_id) REFERENCES public.tile(id) ON DELETE CASCADE;

--
-- Name: plot plot_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plot
    ADD CONSTRAINT plot_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: rate_limit_counter rate_limit_counter_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rate_limit_counter
    ADD CONSTRAINT rate_limit_counter_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organization(id) ON DELETE CASCADE;

--
-- Name: rate_limit_counter rate_limit_counter_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rate_limit_counter
    ADD CONSTRAINT rate_limit_counter_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: resource_access resource_access_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_access
    ADD CONSTRAINT resource_access_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.role(id) ON DELETE CASCADE;

--
-- Name: role role_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role
    ADD CONSTRAINT role_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organization(id) ON DELETE CASCADE;

--
-- Name: role_permission role_permission_permission_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permission
    ADD CONSTRAINT role_permission_permission_id_fkey FOREIGN KEY (permission_id) REFERENCES public.permission(id) ON DELETE CASCADE;

--
-- Name: role_permission role_permission_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permission
    ADD CONSTRAINT role_permission_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.role(id) ON DELETE CASCADE;

--
-- Name: spaces spaces_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spaces
    ADD CONSTRAINT spaces_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organization(id) ON DELETE RESTRICT;

--
-- Name: spaces spaces_owner_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spaces
    ADD CONSTRAINT spaces_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: tab tab_interface_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tab
    ADD CONSTRAINT tab_interface_id_fkey FOREIGN KEY (interface_id) REFERENCES public.interface(id) ON DELETE CASCADE;

--
-- Name: table_tile table_tile_tile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.table_tile
    ADD CONSTRAINT table_tile_tile_id_fkey FOREIGN KEY (tile_id) REFERENCES public.tile(id) ON DELETE CASCADE;

--
-- Name: table_view table_view_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.table_view
    ADD CONSTRAINT table_view_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organization(id) ON DELETE CASCADE;

--
-- Name: table_view table_view_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.table_view
    ADD CONSTRAINT table_view_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.project(id) ON DELETE CASCADE;

--
-- Name: table_view table_view_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.table_view
    ADD CONSTRAINT table_view_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: team_member team_member_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_member
    ADD CONSTRAINT team_member_team_id_fkey FOREIGN KEY (team_id) REFERENCES public.team(id) ON DELETE CASCADE;

--
-- Name: team_member team_member_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_member
    ADD CONSTRAINT team_member_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: team team_organization_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team
    ADD CONSTRAINT team_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organization(id) ON DELETE CASCADE;

--
-- Name: terminal_tile terminal_tile_tile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.terminal_tile
    ADD CONSTRAINT terminal_tile_tile_id_fkey FOREIGN KEY (tile_id) REFERENCES public.tile(id) ON DELETE CASCADE;

--
-- Name: tile tile_tab_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tile
    ADD CONSTRAINT tile_tab_id_fkey FOREIGN KEY (tab_id) REFERENCES public.tab(id) ON DELETE CASCADE;

--
-- Name: user_desktops user_desktops_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_desktops
    ADD CONSTRAINT user_desktops_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: view_tile view_tile_tile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.view_tile
    ADD CONSTRAINT view_tile_tile_id_fkey FOREIGN KEY (tile_id) REFERENCES public.tile(id) ON DELETE CASCADE;

--
-- Name: voices voices_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.voices
    ADD CONSTRAINT voices_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

--
-- Name: shared_platform_routes whatsapp_routes_assistant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shared_platform_routes
    ADD CONSTRAINT whatsapp_routes_assistant_id_fkey FOREIGN KEY (assistant_id) REFERENCES public.assistants(agent_id) ON DELETE CASCADE;

--
-- Name: shared_platform_routes whatsapp_routes_pool_number_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shared_platform_routes
    ADD CONSTRAINT whatsapp_routes_pool_number_id_fkey FOREIGN KEY (pool_number_id) REFERENCES public.shared_pool_numbers(id) ON DELETE CASCADE;

--
-- Project foreign keys (from kernel `project` to platform `user`/`organization`).
-- The core project table is multi-tenant agnostic, so this schema adds
-- the hosted foreign keys after `user` and
-- `organization` exist.
--

ALTER TABLE ONLY public.project
    ADD CONSTRAINT project_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.project
    ADD CONSTRAINT project_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organization(id) ON DELETE CASCADE;

--
--
