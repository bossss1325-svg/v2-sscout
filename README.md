# AUREN SCOUT

Agente de pesquisa comercial V2 e operador de atendimento V3. O Scout coleta e organiza evidências públicas, audita sinais técnicos observáveis de sites e calcula uma pontuação determinística. A V3 é separada da V2, exige consentimento e aprovação humana para envio, e não se integra automaticamente ao Automaton.

## Princípio de execução subordinada

**Capability não é autonomia.** O Scout V2 é um operador subordinado ao AUREN BOSS: conhece as operações disponíveis, mas não decide quando, por que ou em que ordem executá-las. Somente executa uma tarefa explícita recebida do Boss, devolve resultado ou erro e encerra aquela execução.

O AUREN BOSS decide objetivo, cidade/região, categoria, quantidade, pesquisa, abertura de empresa, geração de site e próximos passos. O Scout não inicia prospecção por iniciativa própria, não escolhe localização/categoria/leads, não cria tarefas subsequentes e não toma decisões comerciais. Se não houver tarefa, permanece sem operar o navegador; se os dados obrigatórios estiverem ausentes ou ambíguos, falha explicitamente em vez de inventar valores. Crédito e efeitos externos continuam sujeitos às autorizações explícitas do contrato de cada tarefa.

## Arquitetura

- `scout/discovery.py`: contrato do provedor e catálogo sintético de demonstração.
- `scout/research.py`: valida fontes dos dados, executa auditoria de site e calcula score.
- `scout/website.py`: auditoria HTTP/HTML, com limites de resposta, validação de hosts públicos e respeito a `robots.txt`.
- `scout/scoring.py`: pontuação e classificação sem dependência de rede ou do provedor.
- `scout/storage.py`: `LeadStore` em memória para o fluxo legado e estado JSON durável V2 para tarefas, leads, operador genérico e pacotes entregues ao Auren.
- `scout/v3_models.py`, `scout/v3_executor.py`, `scout/v3_browser.py` e `scout/v3_storage.py`: tarefas de leitura/rascunho/envio V3, adaptador Playwright e histórico/opt-outs persistentes em perfil isolado.
- `scout/service.py`: coordenação, ordenação e logs da pesquisa.
- `scout/api.py` e `scout/cli.py`: interface HTTP e inicialização.
- `scout/browser_controller.py`: conexão e controle isolado do Microsoft Edge via CDP.

O Scout usa a biblioteca padrão do Python, exceto pelo Playwright, necessário para conectar ao endpoint CDP existente. Ele não instala nem inicia outro navegador.

## Instalação e execução

Requer Python 3.10 ou superior. Instale a dependência do Browser Controller:

```sh
python3 -m pip install -r requirements.txt
```

Inicie a API do Scout como antes:

```sh
python3 -m scout
```

A API inicia em `http://127.0.0.1:8080`; Ctrl+C encerra o servidor. As variáveis são lidas do ambiente do processo; `.env.example` é uma referência e não é carregado automaticamente.

## Executável Windows

Gerar no Windows (o PyInstaller não faz cross-build):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-exe.ps1
```

O resultado é `dist\AurenScout-V2.exe`. O nome separado evita sobrescrever um `AurenScout.exe` preexistente. Executar:

```powershell
.\dist\AurenScout-V2.exe
```

O projeto mantém três instâncias isoladas: **V1 = Fortaleza = CDP `127.0.0.1:9222`**, com o perfil `EdgeProfile`; **V2 = AIVIO = CDP `127.0.0.1:9223`**, com o perfil `EdgeProfile-V2`; **V3 = WhatsApp Web = CDP `127.0.0.1:9224`**, com o perfil `EdgeProfile-V3`. Os scripts de inicialização são separados e não reutilizam perfis.

O processo mantém a API local ativa até Ctrl+C. Verificar em outro PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health
```

A resposta deve indicar `status: ok` e `operational: true`. Para desenvolvimento no Windows, use `.\scripts\run-scout.ps1`.

O Browser Controller é executado sob demanda, sem abrir ou autenticar o Edge. O usuário deve iniciar o Microsoft Edge com depuração remota habilitada, entrar manualmente no site desejado e manter o navegador aberto. Depois, consulte o estado via CDP:

```sh
python3 -m scout browser-status
```

O V2 usa `http://127.0.0.1:9223` por padrão. Para a instância V1, use o executável V1 já instalado e o endpoint `http://127.0.0.1:9222`.

Para solicitar explicitamente uma navegação na aba ativa:

```sh
python3 -m scout browser-navigate https://example.com/
```

O comando de status retorna conexão, navegador, abas com título/URL e aba ativa em JSON. Navegação aceita somente URLs HTTP(S) sem credenciais; nenhuma URL é aberta automaticamente. Não exponha o endpoint CDP a redes não confiáveis.

Para diagnosticar somente a página atualmente ativa da V2, sem clique, preenchimento ou navegação:

```powershell
.\dist\AurenScout-V2.exe browser-inspect
```

O comando retorna JSON com título, URL, texto visível (limitado a 12.000 caracteres), inputs visíveis não ocultos/não password, botões e links. Inputs incluem id, role, autocomplete, labels associados e atributos `data-*` não sensíveis; valores de campos identificados como sensíveis não são incluídos, e query strings/fragments de URLs são removidos.

Para inspecionar os atributos dos campos e abrir apenas o dropdown “Escolha o ramo” para diagnóstico (sem selecionar opção ou acionar “Buscar”):

```powershell
.\dist\AurenScout-V2.exe browser-inspect-category
```

O comando retorna os inputs visíveis e seus atributos/labels e compara os elementos DOM antes/depois de abrir o dropdown. A única interação é clicar exatamente uma vez no botão único “Escolha o ramo”; não preenche campos nem seleciona opções. Ele não valida a seleção de categoria no Edge real.

Para validar no Edge V2 a seleção real da categoria recebida, sem iniciar uma pesquisa:

```powershell
.\dist\AurenScout-V2.exe browser-test-category --category "Restaurantes, padarias e lanchonetes"
```

O comando conecta ao CDP V2, exige que a aba ativa seja reconhecida como AIVIO e chama a mesma rotina de seleção usada por `SEARCH_LEADS`: localiza o combobox por `role="combobox"`/`data-slot="select-trigger"` independentemente do texto atual, abre o dropdown e percorre as opções rolando somente o container do próprio menu até encontrar a categoria recebida em `--category` ou chegar ao fim/limite seguro. A seleção só é confirmada quando o controle mostra a categoria recebida. O comando não digita para pesquisar, não clica em “Buscar” e não consome créditos. O resultado JSON informa conexão, reconhecimento do AIVIO e confirmação da categoria; em caso de falha, retorna código de saída diferente de zero.

Para testar o autocomplete de cidade da V2 sem selecionar uma sugestão ou iniciar uma busca:

```powershell
.\dist\AurenScout-V2.exe browser-test-city --city "Porto Alegre"
```

Passe a cidade que deseja diagnosticar em `--city`. O comando conecta ao CDP V2, localiza o input pelo placeholder `Digite uma cidade...`, clica, preenche o valor recebido sem substituí-lo ou abreviá-lo, aguarda o texto exato da cidade e retorna em JSON as linhas visíveis iniciadas pelo valor solicitado e a contagem de correspondências exatas. Não clica em sugestões, categoria ou “Buscar” e não executa nenhuma tarefa. A leitura textual é diagnóstica; ainda não valida no Edge real que as linhas lidas pertencem ao popup de autocomplete.

O `BrowserController` também oferece primitivas explícitas reutilizáveis para clique simples/duplo, formulários, dropdowns, teclado/clipboard, navegação e abas, rolagem, espera por elemento/texto/URL/mudança, leitura de atributos/tabelas/listas, estado, downloads e upload de arquivo explicitamente solicitado. A resolução prioriza role/nome acessível, texto exato, labels/placeholder e atributos estáveis; alvos visíveis ambíguos falham. Classes CSS não são usadas como prioridade. O Browser Controller e o operador genérico V2 bloqueiam WhatsApp; somente o adaptador V3 dedicado em 9224 possui ações de conversa. Os fluxos V2 `SEARCH_LEADS`, `OPEN_COMPANY` e `GENERATE_SITE` usam as primitivas V2. A ausência ou ambiguidade de um controle obrigatório falha explicitamente.

Para tarefas operacionais genéricas enviadas pelo AUREN, a API expõe `POST /operator/tasks` e `GET /operator/tasks/{task_id}`. Esse fluxo persistente usa o Edge V2 em 9223, não exige reconhecer AIVIO e retém ações/evidências após reinício. O Auren pode enviar uma sequência explícita `actions` ou uma pesquisa declarativa com `query`, `goal`, `quantity` (1–10), `niche`, `locality`, `inclusion_criteria`, `exclusion_criteria` e `fields_of_interest`; a V2 planeja `search` seguido de `read_records`. `search` identifica exatamente um campo pelo tipo/labels/placeholder/nome, usa um botão único reconhecível ou Enter somente em `type=search` e espera texto novo estável. `wait_stable` exige hash textual diferente e estável; URL sozinha não conclui a espera. `read_records` lê somente roles `article`/`listitem`; ausência desses records gera pacote parcial/vazio, nunca leads inventados. As demais ações explícitas incluem navegação, abas, menus, controles, leitura, rolagem, espera e verificação. Alvos ambíguos deixam a tarefa `needs_human`/HTTP 202; a API não repete a tarefa.

Resultados estruturados de `/tasks` e do operador genérico são empacotados com `package_id` UUIDv5 determinístico, objetivo, critérios, data/estado, origem e evidência de cada lead, campos ausentes/não verificados, exclusões, duplicatas, erros, limitações e resumo humano. Tarefas, histórico de ações, progresso e pacotes persistem em `SCOUT_V2_STATE_PATH`; tarefas interrompidas viram `failed`, preservam ações concluídas e não retomam o navegador automaticamente. O Auren consulta `GET /v2/results/pending`, recupera por `GET /v2/results/{package_id}` e confirma entrega com `POST /v2/results/{package_id}/delivery` e `{"status":"delivered"}`; falha transitória usa `{"status":"failed","detail":"..."}`. Ack `delivered` repetido é idempotente. Essa interface não chama um serviço remoto do Auren: o consumidor deve buscar e confirmar o pacote.

Exemplo declarativo de tarefa de pesquisa para `POST /operator/tasks`:

```json
{
  "task_id": "2c6b72b1-68a4-4a51-bf59-68aa96832ad5",
  "goal": "Encontrar restaurantes locais com atendimento no salão",
  "query": "restaurantes Porto Alegre",
  "quantity": 10,
  "niche": "restaurantes",
  "locality": "Porto Alegre, RS",
  "inclusion_criteria": ["atende no salão"],
  "exclusion_criteria": ["rede nacional"],
  "fields_of_interest": ["company_name", "phone", "address", "website", "source_url"]
}
```

Critérios são comparados exclusivamente ao conteúdo textual observado. Correspondência negativa explícita é registrada como evidência de negação; critérios que não apareçam ou que não possam ser inferidos com segurança ficam `unverified`, não são presumidos. A pesquisa genérica lê até a quantidade pedida da página semântica atual; paginação ou abertura de resultados adicionais precisa de ações declaradas e verificadas, não ocorre por iniciativa própria.

A memória operacional é opcional e só registra sequências explicitamente nomeadas pelo AUREN após execução integral bem-sucedida. Por exemplo, acrescente `"memory": {"save_as": "home-page-check"}` a uma tarefa com `actions` para registrá-la, e depois envie uma nova tarefa com `task_id` e `"memory": {"reuse": "home-page-check"}` para solicitar sua execução explícita. A memória fica somente no processo da API, mantém os resultados da execução e se perde quando o processo reinicia; não define objetivos, decide ações ou executa tarefas sozinha.

A V2 usa uma entrada exclusiva com CDP `127.0.0.1:9223`; a classe compartilhada e os fluxos da V1 permanecem inalterados. Navegação para pagamentos/serviços de comunicação, interação com controles de pagamento/mensagens e `Enter` em controles de submissão são bloqueados.

## Execução local no Windows

O comando que controla o Edge precisa rodar no mesmo Windows do navegador. Um terminal do GitHub Codespaces, WSL ou outro container não compartilha o `127.0.0.1` do Windows; execute estes passos em um PowerShell local, dentro de uma cópia do repositório clonada no Windows.

1. Instale Python para Windows conforme a política da sua máquina e abra PowerShell na pasta do repositório.
2. Instale as dependências do projeto:

   ```powershell
   py -m pip install -r requirements.txt
   ```

3. Para iniciar o Edge da V1 (Fortaleza), mantenha o script e o perfil existentes:

   ```powershell
   .\scripts\start-edge-cdp.ps1
   ```

   Esse script mantém a V1 em `127.0.0.1:9222` e usa `%LOCALAPPDATA%\AurenScout\EdgeProfile`. Não o altere para iniciar a V2.

4. Para iniciar o Edge da V2, use exclusivamente o script novo:

   ```powershell
   .\scripts\start-edge-cdp-v2.ps1
   ```

   O script V2 fixa CDP em `127.0.0.1:9223` e o perfil em `C:\Users\gabriela.pacheco\AppData\Local\AurenScout\EdgeProfile-V2`. Ele não lê, altera ou reutiliza `EdgeProfile`. Se o site de destino exigir autenticação, faça login manualmente no perfil V2 e mantenha o Edge aberto. Depois, verifique e consulte o Edge V2:

   ```powershell
   Invoke-RestMethod http://127.0.0.1:9223/json/version | ConvertTo-Json -Depth 4
   $env:SCOUT_CDP_ENDPOINT = "http://127.0.0.1:9223"
   .\dist\AurenScout-V2.exe browser-status
   ```

   Para validar os comandos sem executar uma tarefa AIVIO:

   ```powershell
   .\dist\AurenScout-V2.exe browser-status
   .\dist\AurenScout-V2.exe browser-inspect
   .\dist\AurenScout-V2.exe browser-test-city --city "Porto Alegre"
   ```

   `browser-test-city --city "Porto Alegre"` clica no input e digita exatamente a cidade recebida; não seleciona sugestão nem clica em “Buscar”. Substitua o argumento por qualquer cidade enviada pelo AUREN BOSS. O `browser-status` do V2 usa `http://127.0.0.1:9223` por padrão e rejeita o endpoint V1. O status informa conexão, versão reportada por CDP, abas e endpoint; o health da integração também informa o perfil V2 esperado (`EdgeProfile-V2`), sem afirmar que CDP comprovou o diretório de perfil efetivamente usado.

5. Para iniciar o Edge V3, use o perfil isolado e a porta exclusiva:

  ```powershell
  .\scripts\start-edge-cdp-v3.ps1
  Invoke-RestMethod http://127.0.0.1:9224/json/version | ConvertTo-Json -Depth 4
  ```

  O perfil é `%LOCALAPPDATA%\AurenScout\EdgeProfile-V3`; o script não reutiliza V1/V2 e não remove dados. Abra e autentique manualmente o WhatsApp Web. A API V3 (`/v3/health`, `/v3/tasks`) só se conecta ao perfil existente quando uma tarefa é solicitada; não abre páginas nem inicia conversas automaticamente.

   Para executar `browser-status` V1 com a aplicação V1 já instalada, mantenha o endpoint em `http://127.0.0.1:9222`; não use o executável V2 para se conectar ao Edge V1. Se o projeto estiver em Codespaces, faça um clone local no Windows e execute os comandos a partir dele — não execute o cliente CDP no terminal remoto.

O script não cria túneis, não altera firewall e não autentica no AIVIO. O endpoint CDP dá controle elevado sobre a instância do navegador: mantenha-o vinculado ao loopback, não encaminhe a porta e não o exponha à rede/internet. Se a porta estiver ocupada ou o Edge não puder ser localizado pelo registro, o script para com erro em vez de escolher outro executável ou iniciar outro navegador.

## Fluxos legados AIVIO (site de teste da V2)

O AIVIO é somente um site de teste para estes fluxos específicos; não é requisito do operador web genérico `/operator/tasks`. Esses fluxos recebem tarefas explícitas do AUREN, usam a aba ativa do Edge via CDP local e devolvem JSON padronizado. O login no AIVIO continua manual. O Scout não decide quando gerar site nem executa ações comerciais.

Antes de executar tarefas, inicie o Edge CDP como descrito acima, abra o AIVIO na aba ativa e faça login manualmente. O campo de localização observado é identificado por `placeholder="Digite uma cidade..."`; o Scout clica nele, preenche a cidade, aguarda a sugestão textual exata e clica nessa sugestão antes de seguir. Não usa o input de ID dinâmico como campo de Estado nem seleciona por posição. A busca estruturada ainda recebe `state` no contrato e o preserva como dado do lead, mas não tenta preencher um controle de Estado não confirmado nem deriva uma UF da cidade sem evidência retornada pelo AIVIO. A categoria observada é o combobox “Escolha o ramo”; a seleção usa a primitiva genérica e falha em ausência/ambiguidade. O botão observado “Buscar” tem compatibilidade com o rótulo legado “Ver agora”. Os nomes e URLs de leads são observações do AIVIO; campos não disponíveis permanecem `null`. A paginação usa controles visíveis com rótulos acessíveis reconhecíveis e interrompe páginas repetidas. A geração usa apenas ações visíveis identificadas por rótulos observáveis e agora requer uma mensagem textual de sucesso observável; apenas um link novo não confirma geração.

### API de tarefas

`POST /tasks` aceita `SEARCH_LEADS`, `OPEN_COMPANY`, `GENERATE_SITE` e `HEALTH_CHECK`. As respostas contêm `task_id`, `type`, `status`, `result`, `error`, `started_at` e `finished_at`, além dos campos de compatibilidade `data`, `leads`, `artifacts`, `errors` e `warnings`. O executor serializa as tarefas de browser entre processos que usam o mesmo CDP; tarefas aguardando esse lock ficam `queued` e, ao iniciar, passam a `running`. Tarefas, progresso e leads V2 são persistidos em JSON e podem ser consultados após reiniciar o processo. Tarefas que estavam em execução durante a interrupção são marcadas como falhas, sem repetição automática. Um `task_id` persistido não pode ser reutilizado. Falhas retornam status HTTP de erro; timeouts retornam `timeout`/HTTP 504.

O consumo de créditos e efeitos externos não são autorizados por padrão. Para `SEARCH_LEADS`, cada lote exige `authorization.allow_credit_consumption: true` e `authorization.authorized_by: "auren"`; a quantidade máxima é 10 e cada tarefa autorizada inicia no máximo um lote. Para busca de empresa sem URL, `authorization.allow_credit_consumption` precisa ser `true`. Para `GENERATE_SITE`, `authorization.allow_credit_consumption` e `authorization.allow_external_effects` precisam ser `true`. Tarefas sem as permissões exigidas são rejeitadas.

Exemplo `SEARCH_LEADS`:

```json
{
  "task_id": "2c6b72b1-68a4-4a51-bf59-68aa96832ad5",
  "type": "SEARCH_LEADS",
  "authorization": {
    "allow_credit_consumption": true,
    "authorized_by": "auren"
  },
  "payload": {
    "city": "Porto Alegre",
    "state": "RS",
    "category": "restaurantes",
    "quantity": 10,
    "filters": {}
  }
}
```

`quantity` aceita de 1 a 10 leads por lote. Cada novo lote exige uma nova tarefa explicitamente autorizada pelo AUREN. `filters.has_website` é opcional; resultados sem evidência explícita de site não são tratados como empresas sem site. Filtros desconhecidos são indicados em `warnings`, não aplicados silenciosamente.

Exemplos de execução local (substitua o lead de `OPEN_COMPANY`/`GENERATE_SITE` por um lead observado no AIVIO):

```powershell
python -m scout task SEARCH_LEADS --authorization '{"allow_credit_consumption":true,"authorized_by":"auren"}' --payload '{"city":"Porto Alegre","state":"RS","category":"restaurantes","quantity":10,"filters":{}}'
python -m scout task OPEN_COMPANY --payload '{"lead":{"company_name":"Restaurante Exemplo","city":"Porto Alegre","state":"RS","company_url":"https://app.aivio.example/company/123"}}'
python -m scout task GENERATE_SITE --authorization '{"allow_credit_consumption":true,"allow_external_effects":true}' --payload '{"lead":{"company_name":"Restaurante Exemplo","city":"Porto Alegre","state":"RS","company_url":"https://app.aivio.example/company/123"}}'
```

O comando `HEALTH_CHECK` pode ser executado via `/tasks`; `GET /health` informa separadamente `api_operational`, `browser_connected`/`edge_connected`, navegador, versão retornada por CDP, `active_tab`, `aivio_available`, `aivio_hostname`, `aivio_url`, endpoint CDP, perfil V2 esperado e `operational`. `expected_edge_profile` é a configuração esperada (`EdgeProfile-V2`), não uma prova do diretório de perfil efetivamente aberto pelo Edge. A API pode estar operante mesmo quando Edge/AIVIO não está; nesse caso, `operational` será `false`. O fechamento do Edge/AIVIO é reportado como indisponibilidade e não derruba a API.

Para executar uma tarefa via HTTP, envie o objeto JSON acima a `http://127.0.0.1:8080/tasks`. Falhas retornam `status: "failed"` com `error.code`/`error.message` e HTTP 500; timeouts retornam `status: "timeout"` e HTTP 504. Uma busca que não alcance a quantidade pedida retorna `partial`.

Consulte uma tarefa, inclusive enquanto aguarda execução, com `GET http://127.0.0.1:8080/tasks/{task_id}`. Tarefas desconhecidas retornam HTTP 404; IDs duplicados retornam HTTP 409.

`SCOUT_AIVIO_GENERATION_TIMEOUT_MS` controla o timeout de espera por um sinal de conclusão (padrão 120000 ms; intervalo permitido de 1000 a 600000). A geração retorna artefatos observados como website ou URL PDF; um caminho local só será informado quando realmente disponível.

Como o AIVIO não fornece neste repositório um contrato estável de DOM/URL, a interação usa rótulos acessíveis e estrutura HTML semântica visível. A extração de resultados usa roles `article`/`listitem`; se não estiverem presentes, não há evidência de resultado estruturado. A geração não foi validada contra uma sessão AIVIO real neste ambiente. Não foram executadas buscas reais nem geração de sites; esses fluxos foram testados com mocks. Se o controle acessível, os roles ou o sinal de sucesso não forem reconhecidos, a tarefa falha explicitamente ou retorna aviso, em vez de inventar um resultado. **Seleção da cidade/UF, categoria, paginação e extração ainda precisam de validação com AIVIO real no Windows**; o comando `browser-test-city` não seleciona a cidade nem aciona busca.

## Operação V3 de conversas

A V3 usa exclusivamente `http://127.0.0.1:9224` e `EdgeProfile-V3`. No Windows, o script `scripts/start-edge-cdp-v3.ps1` inicia o Edge em loopback e se recusa a iniciar se a porta já estiver ocupada; ele não apaga nem reutiliza perfis existentes. Faça login manualmente no WhatsApp Web. A V3 não abre conversas por busca de contato: `READ_CONVERSATION` lê somente a aba WhatsApp ativa e exige que o título da conversa corresponda exatamente ao `recipient_id` fornecido, com limite de até 50 mensagens.

`POST /v3/tasks` aceita `READ_CONVERSATION`, `PREPARE_MESSAGE`, `SEND_MESSAGE` e `RECORD_OPT_OUT`. Toda tarefa exige `authorization.authorized_by: "auren"`. Rascunhos exigem `consent_verified: true` e uma origem de consentimento descritiva; preparar um rascunho não toca no navegador nem envia mensagem. O envio é uma tarefa separada e exige `allow_external_effects: true`, `human_approved: true` e o `approval_id` retornado com o rascunho. O adaptador confere a conversa ativa, o campo de composição e um único botão de envio; após o clique, exige observar a mensagem de saída antes de registrar `sent`. Se a confirmação for incerta, bloqueia repetição automática.

Opt-outs registrados por `RECORD_OPT_OUT` ficam persistidos; frases inequívocas de interrupção encontradas em mensagens recebidas durante uma leitura também registram opt-out e bloqueiam novos rascunhos/envios. A deduplicação impede reutilizar mensagem igual para o mesmo destinatário. Há intervalo mínimo padrão de 60 segundos entre envios e teto de 10 por hora; exceder os limites exige intervenção humana. `GET /v3/tasks/{task_id}`, `GET /v3/messages/{message_id}` e `GET /v3/history/{recipient_id}` consultam tarefas e histórico persistidos.

Exemplo de preparo sem envio:

```json
{
  "task_id": "2c6b72b1-68a4-4a51-bf59-68aa96832ad5",
  "type": "PREPARE_MESSAGE",
  "authorization": {
    "authorized_by": "auren",
    "consent_verified": true
  },
  "payload": {
    "recipient_id": "Nome exato da conversa ativa",
    "body": "Mensagem aprovada para revisão",
    "consent_source": "Origem verificável do consentimento"
  }
}
```

O contrato V3, a API e as regras de opt-out/ritmo foram exercitados offline com adaptadores falsos. A porta 9224, o login do WhatsApp Web, os seletores DOM, a leitura real e a confirmação de envio ainda não foram validados em navegador; nenhum envio real foi executado. `authorized_by` e `human_approved` são declarações recebidas pela API local, não uma autenticação criptográfica do Auren ou da pessoa aprovadora; mantenha a API em loopback e restrinja quem pode chamá-la.

## Configuração

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `SCOUT_PROVIDER` | `mock` | Provedor disponível nesta versão. Qualquer outro valor é rejeitado. |
| `SCOUT_HOST` | `127.0.0.1` | A API só aceita o loopback `127.0.0.1`. |
| `SCOUT_PORT` | `8080` | Porta HTTP. |
| `SCOUT_LOG_LEVEL` | `INFO` | Nível de log. |
| `SCOUT_CDP_ENDPOINT` | `http://127.0.0.1:9223` no V2; V1 permanece em `http://127.0.0.1:9222` | Endpoint CDP local da versão correspondente. O V2 aceita somente seu endereço loopback HTTP em `127.0.0.1:9223`; a V1 permanece isolada em `127.0.0.1:9222`. |
| `SCOUT_V2_STATE_PATH` | `~/.auren-scout-v2/state.json` | Caminho do arquivo JSON persistente exclusivo da V2. |
| `SCOUT_V3_STATE_PATH` | `~/.auren-scout-v3/state.json` | Caminho do arquivo JSON persistente exclusivo da V3. |
| `SCOUT_AIVIO_GENERATION_TIMEOUT_MS` | `120000` | Timeout da espera condicional da geração do site (1000–600000 ms). |

O Browser Controller valida que o endpoint fala com Microsoft Edge e não armazena credenciais, cookies ou tokens. Não configure credenciais na URL do endpoint.

## API

### `POST /scout/search`

Entrada:

```json
{
	"query": "dentistas",
	"city": "Porto Alegre",
	"state": "RS",
	"limit": 20
}
```

`query`, `city` e `state` são obrigatórios; `limit` aceita inteiros de 1 a 100. A resposta contém `results`, `total` e `errors`. Os resultados são ordenados por score decrescente.

### `GET /scout/lead/:id`

Retorna o registro completo que foi armazenado durante a execução atual do processo. `GET /health` verifica a disponibilidade da API.

### `POST /tasks`

Executa uma tarefa V2 estruturada. Os tipos aceitos são `SEARCH_LEADS`, `OPEN_COMPANY`, `GENERATE_SITE` e `HEALTH_CHECK`. A rota é adicional; `/scout/search` e `/scout/lead/:id` permanecem disponíveis com seus contratos atuais.

### `GET /tasks/{task_id}`

Retorna o estado, progresso e resultado persistidos da tarefa. Os estados `queued` e `running` podem ser consultados enquanto outra tarefa usa o navegador; `completed`, `partial`, `failed` e `timeout` representam estados terminais. A consulta retorna HTTP 404 se o ID não for conhecido.

Exemplo de consulta:

```sh
curl -X POST http://127.0.0.1:8080/scout/search \
	-H 'Content-Type: application/json' \
	-d '{"query":"dentistas","city":"Porto Alegre","state":"RS","limit":20}'
```

## Formato do lead

Cada registro inclui `id`, `companyName`, `category`, `city`, `state`, `phone`, `whatsapp`, `website`, `websiteStatus`, `websiteScore`, `websiteAnalysis`, `instagram`, `address`, `source`, `sources`, `opportunityScore`, `opportunityLevel`, `reasons`, `collectedAt` e `signals`. Valores sem evidência permanecem `null`, `not_verified` ou ausentes em `signals`; fontes acompanham os campos fornecidos pelo provedor. O catálogo atual retorna empresas fictícias, marcadas com `source: "mock://catalog"`.

## Pontuação

Regras centralizadas em `scout/scoring.py`; pontos só são atribuídos para evidências positivas/negativas explícitas. Desconhecido não equivale a falso.

| Evidência | Pontos |
| --- | ---: |
| Site confirmado como não encontrado | +40 |
| Site inacessível | +35 |
| Sinais de site desatualizado | +12 |
| Mobile não compatível | +10 |
| CTA não identificado | +8 |
| WhatsApp não identificado | +4 |
| Empresa ativa, presença pública forte ou reputação positiva | +8, +6 ou +6 |
| Categoria comercial relevante | +5 |

A soma é limitada a 100. Classificação: `low` 0–39, `medium` 40–69, `good` 70–84 e `high` 85–100. Os sinais de atividade, avaliações e presença pública só podem ser marcados quando o provedor trouxer evidência e fonte.

## Testes

```sh
python3 -m unittest discover -s tests -v
```

Os testes unitários simulam o transporte CDP e não exigem Edge ativo. Para verificar a instância Edge V2 real já aberta e autorizada pelo usuário, execute `python3 -m scout browser-status`; o padrão é `http://127.0.0.1:9223`. O resultado deve conter `"connected": true`, `"browser": "Microsoft Edge"`, `tabs` com título/URL e `activeTab`. Não tente autenticar nem abrir sites automaticamente.

O teste de empacotamento executa quando PyInstaller está instalado (`python3 -m pip install -r requirements-build.txt`); no Windows, ele verifica que o artefato `.exe` é gerado.

Os testes mockados da integração AIVIO rodam na mesma suíte. O teste real, que preenche a cidade e clica para pesquisar (podendo consumir créditos), exige opt-in separado e uma sessão AIVIO já aberta e autenticada manualmente no Edge local:

```powershell
$env:AIVIO_LIVE_TEST = "1"
$env:AIVIO_TEST_CITY = "Porto Alegre"
$env:AIVIO_ALLOW_CREDIT_CONSUMPTION = "1"
python -m unittest discover -s tests -p "test_aivio_live.py" -v
```

Acrescente `$env:AIVIO_OPEN_FIRST_COMPANY = "1"` para abrir o primeiro perfil encontrado. Não configure essas variáveis sem autorização explícita para uma busca real.

Para executar uma tarefa V2 real específica, configure `SCOUT_AIVIO_LIVE_TASK` e `SCOUT_AIVIO_LIVE_PAYLOAD` e habilite `SCOUT_AIVIO_LIVE_TEST=1`. `SEARCH_LEADS` e `OPEN_COMPANY` sem URL exigem também `SCOUT_AIVIO_LIVE_ALLOW_CREDIT_CONSUMPTION=1`. `GENERATE_SITE` exige adicionalmente `SCOUT_AIVIO_LIVE_ALLOW_EXTERNAL_EFFECTS=1`. O teste é ignorado se as permissões específicas não estiverem definidas:

```powershell
$env:SCOUT_AIVIO_LIVE_TEST = "1"
$env:SCOUT_AIVIO_LIVE_TASK = "SEARCH_LEADS"
$env:SCOUT_AIVIO_LIVE_PAYLOAD = '{"city":"Porto Alegre","state":"RS","category":"restaurantes","quantity":5,"filters":{}}'
$env:SCOUT_AIVIO_LIVE_ALLOW_CREDIT_CONSUMPTION = "1"
python -m unittest discover -s tests -p "test_aivio_live.py" -v
```

O teste de integração real do BrowserController fica ignorado quando `SCOUT_CDP_ENDPOINT` não está configurado:

```sh
SCOUT_CDP_ENDPOINT=http://127.0.0.1:9223 python3 -m unittest discover -s tests -p 'test_browser_controller_live.py' -v
```

Esse teste de conexão não executa `SEARCH_LEADS`. O teste real de AIVIO em `test_aivio_live.py`, ao contrário, exige os opt-ins descritos acima e pode consumir créditos.

## Limitações

- `/scout/search` continua usando o catálogo sintético para demonstração. A busca real do AIVIO existe somente no executor de tarefas V2 (`/tasks` ou `python -m scout task SEARCH_LEADS`).
- A estrutura DOM, os campos de pesquisa, paginação e os rótulos de geração dependem da interface atual do AIVIO; não foi possível confirmar os seletores em uma sessão real neste ambiente. Execute os testes live opt-in no Edge local antes de depender operacionalmente desses fluxos.
- Não há integração com buscadores, Google Maps, redes sociais ou bases de avaliações.
- A auditoria analisa somente o HTML inicial, respeita `robots.txt`, limita o download a 1 MB e não executa JavaScript. Um site pode bloquear a análise; nesse caso, os dados ficam não verificados ou a análise é marcada como bloqueada.
- Compatibilidade mobile é estimada pela presença de `meta viewport`. Performance é tempo de resposta/download, não um resultado de Lighthouse. Aparência visual permanece não verificada sem navegador.
- Campos não observados, como avaliações, endereço, telefone e Instagram, não são preenchidos por inferência.
- O `LeadStore` legado permanece em memória; tarefas/leads V2 e tarefas/mensagens/opt-outs V3 ficam em arquivos JSON locais separados. A API não tem autenticação de rede; mantenha-a em loopback e restrinja quem pode chamá-la.
- Não há dashboard nem coleta de métricas Google.
- A V3 ainda não foi validada contra uma sessão real do WhatsApp Web; o DOM/seletores podem exigir ajustes no perfil V3. A validação do Edge V2 em 9223 também permanece pendente neste ambiente.

## Integração futura

Um adaptador pode implementar `SearchProvider` em `scout/discovery.py`, respeitando os termos, limites e políticas da fonte. Ele deve retornar somente fatos com atribuição em `sources`; `CompanyResearcher` rejeita registros sem origem nos campos essenciais e remove valores opcionais/sinais sem fonte. As APIs V2/V3 aceitam tarefas explícitas que podem ser chamadas pelo Auren; não existe neste repositório um gerente Auren/Automaton que coordene as tarefas automaticamente. A ligação HTTP real entre esses componentes ainda precisa ser integrada e validada.