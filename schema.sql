-- Esquema do monitor de preços (PostgreSQL 13+)

CREATE TABLE IF NOT EXISTS products (
    id              serial PRIMARY KEY,
    categoria       text,
    nome            text NOT NULL,
    tensao          text,
    loja            text,
    url             text NOT NULL UNIQUE,
    valor_inicial   numeric(12,2),
    ativo           boolean NOT NULL DEFAULT true,
    criado_em       timestamptz NOT NULL DEFAULT now()
);

-- Uma linha por produto por dia.
-- status: ok | suspeito | erro | indisponivel
--   suspeito     = variação grande demais em relação ao último preço bom (confirmar com --approve)
--   erro         = não conseguiu ler o preço (bloqueio, página mudou, timeout)
--   indisponivel = a página informa que o produto está esgotado
CREATE TABLE IF NOT EXISTS price_snapshots (
    id               bigserial PRIMARY KEY,
    product_id       integer NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    data             date NOT NULL,
    coletado_em      timestamptz NOT NULL DEFAULT now(),
    preco_pix        numeric(12,2),
    parcelado_total  numeric(12,2),
    parcelas         integer,
    avaliacao        numeric(3,1),
    qtd_avaliacoes   integer,
    status           text NOT NULL DEFAULT 'ok'
                     CHECK (status IN ('ok','suspeito','erro','indisponivel')),
    origem           text NOT NULL DEFAULT 'coletor',   -- coletor | planilha
    fonte_preco      text,                              -- jsonld | meta | texto | regra-loja
    detalhe          text,
    UNIQUE (product_id, data)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_prod_data ON price_snapshots (product_id, data DESC);
