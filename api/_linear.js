// Linear API helpers. Uses the Linear GraphQL API.
// Requires env var LINEAR_API_KEY, and LINEAR_TEAM_KEY (the short team key, e.g. "POL").
// Docs: https://developers.linear.app/docs/graphql/working-with-the-graphql-api

const LINEAR_API = 'https://api.linear.app/graphql';

async function linearRequest(query, variables) {
  const key = process.env.LINEAR_API_KEY;
  if (!key) throw new Error('LINEAR_API_KEY not set');
  const resp = await fetch(LINEAR_API, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': key, // Linear personal API keys go in Authorization directly
    },
    body: JSON.stringify({ query, variables }),
  });
  const json = await resp.json();
  if (json.errors) {
    throw new Error('Linear API error: ' + JSON.stringify(json.errors));
  }
  return json.data;
}

// Resolve the short team key (e.g. "POL") into the team's UUID.
// Cached in memory for the life of the serverless instance.
let _cachedTeamId = null;
async function resolveTeamId() {
  if (_cachedTeamId) return _cachedTeamId;
  const teamKey = process.env.LINEAR_TEAM_KEY;
  if (!teamKey) throw new Error('LINEAR_TEAM_KEY not set');
  const query = `
    query Teams {
      teams(first: 100) { nodes { id key name } }
    }`;
  const data = await linearRequest(query, {});
  const team = (data.teams?.nodes || []).find(
    t => t.key.toLowerCase() === teamKey.toLowerCase()
  );
  if (!team) {
    throw new Error('No Linear team found with key "' + teamKey + '"');
  }
  _cachedTeamId = team.id;
  return _cachedTeamId;
}

// Create a new issue in the configured team.
export async function createIssue({ title, description }) {
  const teamId = await resolveTeamId();
  const query = `
    mutation IssueCreate($input: IssueCreateInput!) {
      issueCreate(input: $input) {
        success
        issue { id identifier url }
      }
    }`;
  const data = await linearRequest(query, {
    input: { teamId, title, description },
  });
  if (!data.issueCreate || !data.issueCreate.success) {
    throw new Error('Linear issue creation failed');
  }
  return data.issueCreate.issue;
}

// Given a list of Linear issue IDs, return { issueId: workflowStateName }.
export async function getIssueStatuses(ids) {
  if (!ids.length) return {};
  const query = `
    query Issues($ids: [ID!]) {
      issues(filter: { id: { in: $ids } }) {
        nodes { id state { name type } }
      }
    }`;
  const data = await linearRequest(query, { ids });
  const out = {};
  (data.issues?.nodes || []).forEach(n => {
    out[n.id] = n.state?.name || null;
  });
  return out;
}
