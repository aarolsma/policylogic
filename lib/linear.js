// Linear API helpers. Uses the Linear GraphQL API.
// Requires env var LINEAR_API_KEY (and LINEAR_TEAM_ID for createIssue).
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

// Create a new issue in the configured team.
export async function createIssue({ title, description }) {
  const teamId = process.env.LINEAR_TEAM_ID;
  if (!teamId) throw new Error('LINEAR_TEAM_ID not set');
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
