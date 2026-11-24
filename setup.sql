-- =====================================================================================
-- GPTW Employee Survey - AI Theme & Sentiment Analysis on Snowflake Cortex
-- Replication script (same logic as the prototype in GPTW_DEMO.SURVEY)
--
-- Prerequisites
--   * Run as ACCOUNTADMIN (or a role with CREATE DATABASE / WAREHOUSE / COMPUTE POOL /
--     INTEGRATION privileges) -- STEP 6 creates a compute pool and a PyPI integration
--   * SNOWFLAKE.CORTEX_USER database role granted to the role running this
--   * Model claude-sonnet-4-5 available in the account region, or cross-region
--     inference enabled (ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = '...')
--     -> review data-residency implications before enabling
--   * Survey responses already in Snowflake (edit STEP 2 to point at your table)
--   * Theme glossary loaded into THEME_GLOSSARY (STEP 1)
-- =====================================================================================

-- STEP 0: Environment ------------------------------------------------------------------
CREATE DATABASE IF NOT EXISTS GPTW_DEMO;
CREATE SCHEMA IF NOT EXISTS GPTW_DEMO.SURVEY;
CREATE WAREHOUSE IF NOT EXISTS GPTW_WH WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60 AUTO_RESUME = TRUE;
USE WAREHOUSE GPTW_WH;
USE SCHEMA GPTW_DEMO.SURVEY;

-- STEP 1: Theme glossary (hashtag -> GPTW theme -> GPTW dimension) ---------------------
-- Load the mapping sheet (Snowsight "Load data" or COPY INTO) into this table.
CREATE TABLE IF NOT EXISTS THEME_GLOSSARY (
    HASHTAG            VARCHAR,   -- e.g. #FairPay
    DESCRIPTION        VARCHAR,   -- what it captures (used as guidance in the prompt)
    GPTW_DIMENSION     VARCHAR,   -- Credibility / Respect / Fairness / Pride / Camaraderie / ...
    GPTW_THEME         VARCHAR,   -- e.g. Fair Compensation
    RELATED_STATEMENTS VARCHAR    -- GPTW Trust Index statements
);

-- STEP 2: Survey responses --------------------------------------------------------------
-- Option A (recommended): your survey data is already in Snowflake. Skip the CREATE TABLE
-- below and instead create a view that renames your columns, e.g.:
--
--   CREATE OR REPLACE VIEW RAW_RESPONSES AS
--   SELECT company_id::VARCHAR AS COMPANY_ID, login_id AS LOGIN_ID,
--          <improve_question_col>     AS Q_IMPROVE,
--          <great_place_question_col> AS Q_GREAT_PLACE,
--          <manager_level_col> AS MANAGER_LEVEL, <gender_col> AS GENDER, <age_col> AS AGE_BAND,
--          <tenure_col> AS TENURE_BAND, <work_status_col> AS WORK_STATUS,
--          <exec_confidence_col> AS EXEC_CONFIDENCE, <innovation_col> AS INNOVATION_OPPORTUNITIES,
--          <birth_cohort_col> AS BIRTH_COHORT, NULL AS DISABILITY
--   FROM <your_db>.<your_schema>.<your_survey_table>
--   WHERE <survey_cycle_filter>;      -- start with a small sample (a few hundred rows)
--
-- Option B: load a file (e.g. the sample Excel saved as CSV) into the table below using
-- Snowsight > Data > Add Data > Load data into a table.
CREATE TABLE IF NOT EXISTS RAW_RESPONSES (
    COMPANY_ID VARCHAR, LOGIN_ID VARCHAR,
    Q_IMPROVE VARCHAR,        -- "If you could change one thing..."
    Q_GREAT_PLACE VARCHAR,    -- "Is there anything unique or unusual..."
    MANAGER_LEVEL VARCHAR, GENDER VARCHAR, AGE_BAND VARCHAR, TENURE_BAND VARCHAR,
    WORK_STATUS VARCHAR, EXEC_CONFIDENCE VARCHAR, INNOVATION_OPPORTUNITIES VARCHAR,
    BIRTH_COHORT VARCHAR, DISABILITY VARCHAR
);

-- STEP 3: One row per comment -----------------------------------------------------------
CREATE OR REPLACE TABLE COMMENTS AS
SELECT LOGIN_ID || '_' || Q AS COMMENT_ID, LOGIN_ID, COMPANY_ID,
       Q AS QUESTION_TYPE,
       IFF(Q = 'IMPROVE', 'If you could change one thing...', 'What makes it a great place to work...') AS QUESTION_TEXT,
       TRIM(COMMENT_TEXT) AS COMMENT_TEXT,
       MANAGER_LEVEL, GENDER, AGE_BAND, TENURE_BAND, WORK_STATUS, EXEC_CONFIDENCE, INNOVATION_OPPORTUNITIES, BIRTH_COHORT
FROM (SELECT *, Q_IMPROVE AS IMPROVE, Q_GREAT_PLACE AS GREAT_PLACE FROM RAW_RESPONSES)
UNPIVOT (COMMENT_TEXT FOR Q IN (IMPROVE, GREAT_PLACE))
WHERE LENGTH(TRIM(COMMENT_TEXT)) > 0;

-- STEP 4: AI enrichment (one Cortex call per comment) -----------------------------------
-- Tip: test first by adding  "QUALIFY ROW_NUMBER() OVER (ORDER BY COMMENT_ID) <= 10"
CREATE OR REPLACE TABLE COMMENTS_ENRICHED AS
WITH g AS (
  SELECT LISTAGG(HASHTAG || ' = ' || DESCRIPTION || ' [' || GPTW_THEME || ']', '\n') AS GLOSS,
         ARRAY_AGG(HASHTAG) AS TAGS,
         ARRAY_AGG(OBJECT_CONSTRUCT('label', HASHTAG, 'description', DESCRIPTION)) AS LBL
  FROM THEME_GLOSSARY
)
SELECT c.*,
 AI_COMPLETE(
  model => 'claude-sonnet-4-5',
  prompt => 'You analyse employee survey comments for Great Place To Work.\nQuestion asked: ' || c.QUESTION_TEXT ||
   '\n\nTheme glossary (hashtag = what it captures [GPTW theme]):\n' || g.GLOSS ||
   '\n\nRules:\n- Identify EVERY distinct theme discussed; a comment may contain several issues.\n- For each theme give the sentiment the employee expresses about that aspect: positive (praise), negative (complaint / problem / unmet need / request for change), or neutral.\n- A suggestion to change or add something implies the current state is lacking -> negative for that theme.\n- evidence = the shortest verbatim phrase from the comment supporting the theme.\n- Use #NoChangeNeeded / #GeneralPositive / #NonSpecific only when no specific theme applies.\n- risk_flag: attrition (intent to leave), harassment_or_safety, ethics_or_misconduct, discrimination, burnout, or none.\n- sentiment_score from -1 (very negative) to 1 (very positive).\n\nComment:\n"""' || c.COMMENT_TEXT || '"""',
  model_parameters => {'temperature': 0, 'max_tokens': 1200},
  response_format => {'type':'json','schema':{'type':'object','properties':{
     'themes':{'type':'array','items':{'type':'object','properties':{
         'hashtag':{'type':'string','enum': g.TAGS},
         'sentiment':{'type':'string','enum':['positive','negative','neutral']},
         'evidence':{'type':'string'}},'required':['hashtag','sentiment','evidence']}},
     'overall_sentiment':{'type':'string','enum':['positive','negative','neutral','mixed']},
     'sentiment_score':{'type':'number'},
     'risk_flag':{'type':'string','enum':['none','attrition','harassment_or_safety','ethics_or_misconduct','discrimination','burnout']},
     'summary':{'type':'string'}},
   'required':['themes','overall_sentiment','sentiment_score','risk_flag','summary']}}
 ) AS AI_RESULT,
 -- Optional low-cost baseline for comparison
 AI_CLASSIFY(c.COMMENT_TEXT, g.LBL, {'output_mode':'multi',
     'task_description':'Employee survey comment from a Great Place To Work survey. Tag every workplace theme the comment discusses (a single comment can cover several). Use #NoChangeNeeded, #GeneralPositive or #NonSpecific only when no specific theme applies.'}):labels AS CLASSIFY_THEMES,
 AI_SENTIMENT(c.COMMENT_TEXT):categories[0]:sentiment::STRING AS AI_SENTIMENT_LABEL
FROM COMMENTS c, g;

-- Check for failed rows (AI_RESULT IS NULL) and re-run just those if any
SELECT COUNT(*) AS TOTAL, COUNT_IF(AI_RESULT IS NULL) AS FAILED FROM COMMENTS_ENRICHED;

-- STEP 5: Analytics model ---------------------------------------------------------------
CREATE OR REPLACE TABLE COMMENT_THEMES AS
SELECT DISTINCT e.COMMENT_ID, e.LOGIN_ID, e.COMPANY_ID, e.QUESTION_TYPE,
  f.value:hashtag::STRING AS HASHTAG, g.GPTW_THEME, g.GPTW_DIMENSION, g.RELATED_STATEMENTS,
  f.value:sentiment::STRING AS THEME_SENTIMENT, f.value:evidence::STRING AS EVIDENCE,
  e.MANAGER_LEVEL, e.GENDER, e.AGE_BAND, e.TENURE_BAND, e.WORK_STATUS, e.EXEC_CONFIDENCE, e.INNOVATION_OPPORTUNITIES, e.BIRTH_COHORT
FROM COMMENTS_ENRICHED e, LATERAL FLATTEN(e.AI_RESULT:themes) f
JOIN THEME_GLOSSARY g ON g.HASHTAG = f.value:hashtag::STRING;

CREATE OR REPLACE VIEW V_COMMENTS AS
SELECT COMMENT_ID, LOGIN_ID, COMPANY_ID, QUESTION_TYPE, COMMENT_TEXT,
  AI_RESULT:overall_sentiment::STRING AS OVERALL_SENTIMENT,
  AI_RESULT:sentiment_score::FLOAT    AS SENTIMENT_SCORE,
  AI_RESULT:risk_flag::STRING         AS RISK_FLAG,
  AI_RESULT:summary::STRING           AS AI_SUMMARY,
  ARRAY_SIZE(AI_RESULT:themes)        AS NUM_THEMES,
  CLASSIFY_THEMES, AI_SENTIMENT_LABEL,
  MANAGER_LEVEL, GENDER, AGE_BAND, TENURE_BAND, WORK_STATUS, EXEC_CONFIDENCE, INNOVATION_OPPORTUNITIES, BIRTH_COHORT
FROM COMMENTS_ENRICHED;

CREATE OR REPLACE VIEW V_THEME_HEALTH AS
SELECT GPTW_DIMENSION, GPTW_THEME, HASHTAG,
  COUNT(DISTINCT LOGIN_ID) AS RESPONDENTS,
  ROUND(100 * COUNT(DISTINCT LOGIN_ID) / (SELECT COUNT(DISTINCT LOGIN_ID) FROM COMMENTS), 1) AS PREVALENCE_PCT,
  COUNT_IF(THEME_SENTIMENT = 'positive') AS POS,
  COUNT_IF(THEME_SENTIMENT = 'negative') AS NEG,
  COUNT_IF(THEME_SENTIMENT = 'neutral')  AS NEU,
  ROUND(100 * (COUNT_IF(THEME_SENTIMENT = 'positive') - COUNT_IF(THEME_SENTIMENT = 'negative')) / COUNT(*), 0) AS NET_SENTIMENT
FROM COMMENT_THEMES GROUP BY 1, 2, 3;

-- STEP 6: Streamlit in Snowflake app ----------------------------------------------------
-- Upload streamlit_app.py and pyproject.toml (Snowsight > stage > + Files, or PUT/snow CLI)
CREATE STAGE IF NOT EXISTS APP_STAGE DIRECTORY = (ENABLE = TRUE);
-- PUT file://streamlit_app.py @APP_STAGE/app AUTO_COMPRESS=FALSE OVERWRITE=TRUE;
-- PUT file://pyproject.toml   @APP_STAGE/app AUTO_COMPRESS=FALSE OVERWRITE=TRUE;

-- Container runtime needs a compute pool and outbound access to PyPI (to install packages)
CREATE COMPUTE POOL IF NOT EXISTS GPTW_APP_POOL
  MIN_NODES = 1 MAX_NODES = 1 INSTANCE_FAMILY = CPU_X64_XS AUTO_SUSPEND_SECS = 600 AUTO_RESUME = TRUE;

CREATE NETWORK RULE IF NOT EXISTS GPTW_DEMO.SURVEY.PYPI_NETWORK_RULE
  MODE = EGRESS TYPE = HOST_PORT
  VALUE_LIST = ('pypi.org', 'pypi.python.org', 'pythonhosted.org', 'files.pythonhosted.org');

CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS GPTW_PYPI_ACCESS_INTEGRATION
  ALLOWED_NETWORK_RULES = (GPTW_DEMO.SURVEY.PYPI_NETWORK_RULE)
  ENABLED = TRUE;

CREATE OR REPLACE STREAMLIT GPTW_SURVEY_INSIGHTS
  FROM '@GPTW_DEMO.SURVEY.APP_STAGE/app'
  MAIN_FILE = 'streamlit_app.py'
  QUERY_WAREHOUSE = GPTW_WH
  RUNTIME_NAME = 'SYSTEM$ST_CONTAINER_RUNTIME_PY3_11'
  COMPUTE_POOL = GPTW_APP_POOL
  EXTERNAL_ACCESS_INTEGRATIONS = (GPTW_PYPI_ACCESS_INTEGRATION)
  TITLE = 'GPTW Survey AI Insights';
-- Stop the app container after 1 idle day (minimum allowed) so hosting cost is capped
ALTER STREAMLIT GPTW_SURVEY_INSIGHTS SET IDLE_AUTO_SHUTDOWN_TIME_SECONDS = 86400;
-- Open it from Snowsight > Projects > Streamlit > GPTW Survey AI Insights
-- (first start takes a few minutes while packages install)

-- Share with consultants
-- GRANT USAGE ON DATABASE GPTW_DEMO TO ROLE GPTW_ANALYST;
-- GRANT USAGE ON SCHEMA GPTW_DEMO.SURVEY TO ROLE GPTW_ANALYST;
-- GRANT SELECT ON ALL TABLES IN SCHEMA GPTW_DEMO.SURVEY TO ROLE GPTW_ANALYST;
-- GRANT SELECT ON ALL VIEWS  IN SCHEMA GPTW_DEMO.SURVEY TO ROLE GPTW_ANALYST;
-- GRANT USAGE ON STREAMLIT GPTW_DEMO.SURVEY.GPTW_SURVEY_INSIGHTS TO ROLE GPTW_ANALYST;
-- GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE GPTW_ANALYST;   -- for brief & chat
