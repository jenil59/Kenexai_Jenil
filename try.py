
llm_generated_sql = """
   
WITH north_american_companies AS (
  SELECT company_vtr_id, vtr_company_name
  FROM vtr.company
  WHERE company_location_country IN ('United States of America', 'Canada', 'Mexico')
    AND company_active = 'Active'
),
unscripted_content AS (
  SELECT DISTINCT c.content_vtr_id, c.title, c.genres, c.status, 
         STRFTIME(CAST(c.release_date AS timestamp), '%Y-%m-%d') AS release_date,
         c.origin_country, c.genre_category
  FROM vtr.content c
  WHERE c.genre_category = 'unscripted'
    AND (
      EXTRACT(YEAR FROM c.release_date) BETWEEN 2024 AND 2026
      OR EXISTS (
        SELECT 1 FROM vtr.content_seasons cs
        WHERE cs.content_vtr_id = c.content_vtr_id
        AND cs.season_airing_date LIKE '202[4-6]-%'
      )
    )
),
content_with_na_companies AS (
  SELECT DISTINCT uc.content_vtr_id, uc.title, uc.genres, uc.status, 
         uc.release_date, uc.origin_country, nac.company_vtr_id, nac.vtr_company_name
  FROM unscripted_content uc
  JOIN vtr.company_content_relation ccr ON uc.content_vtr_id = ccr.content_vtr_id
  JOIN north_american_companies nac ON ccr.company_vtr_id = nac.company_vtr_id
),
deals_with_na_companies AS (
  SELECT DISTINCT d.deal_vtr_id, d.deal_description, d.deal_type, d.deal_subtype,
         STRFTIME(CAST(d.deal_date AS timestamp), '%Y-%m-%d') AS deal_date,
         d.deal_score_value, de.entity_vtr_id AS content_vtr_id,
         nac.company_vtr_id, nac.vtr_company_name
  FROM vtr.deal d
  JOIN vtr.deal_entities de ON d.deal_vtr_id = de.deal_vtr_id
  JOIN north_american_companies nac ON (
    (UPPER(de.entity_type) = 'COMPANY' AND de.entity_vtr_id = nac.company_vtr_id)
    OR (UPPER(de.entity_type) = 'CONTENT' AND de.relation_type = 'content')
  )
  WHERE CAST(d.deal_date AS DATE) BETWEEN DATE '2024-01-01' AND DATE '2026-12-31'
    AND d.deal_type IN ('production', 'acquisition', 'distribution')
),
combined_results AS (
  SELECT cwc.content_vtr_id, cwc.title, cwc.genres, cwc.status, cwc.release_date,
         cwc.company_vtr_id, cwc.vtr_company_name, 'Production' AS result_type,
         NULL::VARCHAR AS deal_vtr_id, NULL::VARCHAR AS deal_description,
         NULL::VARCHAR AS deal_type, NULL::VARCHAR AS deal_subtype, NULL::VARCHAR AS deal_date,
         NULL::VARCHAR AS deal_score_value
  FROM content_with_na_companies cwc
  UNION ALL
  SELECT uc.content_vtr_id, uc.title, uc.genres, uc.status, uc.release_date,
         dwc.company_vtr_id, dwc.vtr_company_name, 'Deal' AS result_type,
         dwc.deal_vtr_id, dwc.deal_description, dwc.deal_type, dwc.deal_subtype,
         dwc.deal_date, dwc.deal_score_value
  FROM deals_with_na_companies dwc
  LEFT JOIN unscripted_content uc ON dwc.content_vtr_id = uc.content_vtr_id
)
SELECT DISTINCT
  cr.content_vtr_id,
  cr.title,
  cr.genres,
  cr.status,
  cr.release_date,
  cr.company_vtr_id,
  cr.vtr_company_name,
  cr.result_type,
  cr.deal_vtr_id,
  cr.deal_description,
  cr.deal_type,
  cr.deal_subtype,
  cr.deal_date,
  cr.deal_score_value,
  COUNT(*) OVER() AS total_result_count
FROM combined_results cr
ORDER BY cr.release_date DESC NULLS LAST, cr.deal_date DESC NULLS LAST, cr.title ASC
LIMIT 200;

        """
        
        
        
import duckdb
import sqlglot
from sqlglot import exp
import pathlib


class DuckDBLLMValidator:
    def __init__(self, db_path: str):
        self.db_path = str(pathlib.Path(db_path).resolve())
        self.conn = duckdb.connect(database=self.db_path, read_only=True)
        self.schema = self._extract_database_schema()

    def _extract_database_schema(self) -> dict:
        """Dynamically builds the allowed schema directly from DuckDB."""
        schema = {}
        tables_query = (
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_type='BASE TABLE';"
        )
        tables = self.conn.execute(tables_query).fetchall()
        for t in tables:
            table_name = t[0]
            cols_query = f"SELECT column_name FROM information_schema.columns WHERE table_name='{table_name}';"
            cols = self.conn.execute(cols_query).fetchall()
            schema[table_name] = [c[0] for c in cols]
        return schema

    def _check_join_anti_patterns(self, parsed_ast: exp.Expression) -> tuple[bool, str]:
        """
        Inspects the AST for Cartesian products, 'ON TRUE', '1=1', and disconnected joins.
        """
        # 1. Inspect all JOIN nodes
        for join in parsed_ast.find_all(exp.Join):
            # A. Check for explicit CROSS JOIN
            if join.kind and "CROSS" in join.kind.upper():
                return False, f"Anti-Pattern Detected: Explicit CROSS JOIN on '{join.this.name or join.this}'."

            on_clause = join.args.get("on")
            using_clause = join.args.get("using")

            # B. Check for missing conditions (when not using USING or NATURAL)
            if not on_clause and not using_clause and not join.args.get("natural"):
                return False, f"Anti-Pattern Detected: JOIN on '{join.this.name or join.this}' has no ON/USING condition."

            if on_clause:
                on_sql = on_clause.sql(dialect="duckdb").strip().upper()

                # C. Catch trivial predicates (ON TRUE, ON 1=1, ON 1)
                trivial_predicates = {"TRUE", "1", "1=1", "1 = 1", "TRUE=TRUE", "TRUE = TRUE"}
                if on_sql in trivial_predicates or isinstance(on_clause, exp.Boolean):
                    return False, f"Anti-Pattern Detected: Trivial join predicate '{on_sql}' on table '{join.this.name or join.this}'."

                # D. Catch tautologies like (col = col where both sides are identical literals)
                if isinstance(on_clause, exp.EQ) and on_clause.left == on_clause.right:
                    return False, f"Anti-Pattern Detected: Tautological join condition '{on_sql}'."

                # E. Check for Disconnected Joins (Ensure the ON clause references the joined table/alias)
                joined_alias = (join.this.alias or join.this.name or "").lower()
                if joined_alias:
                    column_tables_in_on = {
                        (col.table or "").lower()
                        for col in on_clause.find_all(exp.Column)
                        if col.table
                    }
                    if column_tables_in_on and joined_alias not in column_tables_in_on:
                        return False, (
                            f"Anti-Pattern Detected: Disconnected Join. Table/alias '{joined_alias}' "
                            f"is not referenced in its own ON clause: '{on_sql}'."
                        )

        # 2. Inspect FROM clauses for comma-separated Cartesian joins: FROM a, b
        for from_node in parsed_ast.find_all(exp.From):
            if len(from_node.expressions) > 1:
                return False, "Anti-Pattern Detected: Comma-separated table list (implicit Cartesian product) found in FROM clause."

        return True, "No join anti-patterns found."

    def validate_and_test(self, sql_string: str) -> tuple[bool, str]:
        """Runs the 4-layer validation: AST -> Schema -> Anti-Pattern -> Dry Run"""

        # --- LAYER 1: AST Parsing & Security Allowlist ---
        try:
            parsed_ast = sqlglot.parse_one(sql_string, read="duckdb")
        except sqlglot.errors.ParseError as e:
            return False, f"Syntax Error: {e}"

        if not isinstance(parsed_ast, exp.Select):
            return False, "Security Violation: Only SELECT statements are permitted."

        # --- LAYER 2: Semantic Schema Grounding (CTE-Aware) ---
        cte_names = {cte.alias for cte in parsed_ast.find_all(exp.CTE)}
        extracted_tables = {
            table.name for table in parsed_ast.find_all(exp.Table)
            if table.name not in cte_names
        }

        if not extracted_tables:
            return False, "Semantic Error: No base tables referenced in query."

        # Verify base tables exist in schema (strip catalog prefix if present)
        for table in extracted_tables:
            clean_table = table.split(".")[-1]
            if clean_table not in self.schema and table not in self.schema:
                return False, f"Schema Violation: Table '{table}' does not exist in the database."

        # --- LAYER 3: Join Anti-Pattern & Smell Detection ---
        passed_smell_test, smell_msg = self._check_join_anti_patterns(parsed_ast)
        if not passed_smell_test:
            return False, smell_msg

        # --- LAYER 4: Engine-Level Dry Run ---
        try:
            self.conn.execute(f"EXPLAIN {sql_string}")
        except Exception as e:
            return False, f"Engine Error during Dry-Run: {e}"

        return True, "Success: Query is valid, semantically sound, and ready to execute."

    def execute_safely(self, sql_string: str) -> list[dict]:
        is_valid, msg = self.validate_and_test(sql_string)
        if not is_valid:
            raise ValueError(f"Query blocked: {msg}")

        result = self.conn.execute(sql_string)
        columns = [desc[0] for desc in result.description]
        rows = result.fetchall()
        return [dict(zip(columns, row)) for row in rows]

    def close(self):
        self.conn.close()
if __name__ == "__main__":
    my_db_path = r"D:\learn\Kenexai\vitrina resources\db\backup\vtr.db"

    validator = DuckDBLLMValidator(db_path=my_db_path)

    bad_moriarty_query = llm_generated_sql
    is_valid, msg = validator.validate_and_test(bad_moriarty_query)
    print(f"Validation Result: {msg}")
    validator.close()     
        
        
        
        
        
        
        
        
        
        
        
     
""" WITH moriarty_content AS (
  SELECT content_vtr_id, title
  FROM vtr.content
  WHERE title ILIKE '%Moriarty Rising%' OR title ILIKE '%Sherlock Holmes%'
),
deal_companies AS (
  SELECT DISTINCT
    d.deal_vtr_id,
    d.deal_description,
    d.deal_type,
    d.deal_subtype,
    STRFTIME(CAST(d.deal_date AS timestamp), '%Y-%m-%d') AS deal_date,
    de.entity_vtr_id AS company_vtr_id,
    de.relation_type,
    mc.content_vtr_id,
    mc.title
  FROM vtr.deal d
  JOIN vtr.deal_entities de ON d.deal_vtr_id = de.deal_vtr_id
  JOIN moriarty_content mc ON TRUE
  WHERE UPPER(de.entity_type) = 'COMPANY'
    AND d.deal_type ILIKE '%production%'
),
company_details AS (
  SELECT
    dc.company_vtr_id,
    c.vtr_company_name,
    c.primary_company_type_name,
    c.company_location_country,
    c.reputation,
    dc.deal_vtr_id,
    dc.deal_description,
    dc.deal_type,
    dc.deal_subtype,
    dc.deal_date,
    dc.relation_type,
    dc.title,
    dc.content_vtr_id
  FROM deal_companies dc
  JOIN vtr.company c ON dc.company_vtr_id = c.company_vtr_id
),
company_persons AS (
  SELECT
    cd.company_vtr_id,
    cd.vtr_company_name,
    cd.primary_company_type_name,
    cd.company_location_country,
    cd.reputation,
    cd.deal_vtr_id,
    cd.deal_description,
    cd.deal_type,
    cd.deal_subtype,
    cd.deal_date,
    cd.relation_type,
    cd.title,
    cd.content_vtr_id,
    p.person_vtr_id,
    p.name,
    p.email_id,
    p.linkedin_url,
    cpr.designation,
    p.role_specialization,
    p.sub_role_specialization
  FROM company_details cd
  LEFT JOIN vtr.company_person_relation cpr ON cd.company_vtr_id = cpr.company_vtr_id
  LEFT JOIN vtr.person p ON cpr.person_vtr_id = p.person_vtr_id
)
SELECT
  cp.vtr_company_name,
  cp.company_vtr_id,
  cp.primary_company_type_name,
  cp.company_location_country,
  cp.reputation,
  cp.relation_type AS company_role_in_deal,
  cp.deal_type,
  cp.deal_subtype,
  cp.deal_date,
  cp.title AS content_title,
  cp.content_vtr_id,
  cp.name AS person_name,
  cp.person_vtr_id,
  cp.designation,
  cp.role_specialization,
  cp.sub_role_specialization,
  cp.email_id,
  cp.linkedin_url,
  COUNT(*) OVER() AS total_result_count
FROM company_persons cp
ORDER BY cp.vtr_company_name, CASE WHEN cp.email_id IS NOT NULL AND TRIM(cp.email_id) != '' THEN 0 ELSE 1 END, cp.reputation DESC NULLS LAST
LIMIT 200;
"""