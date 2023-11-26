# Import necessary libraries
import requests
import google.auth
import google.auth.transport.requests
from google.oauth2 import service_account


class VertexLlama:
    def __init__(self, project_id, endpoint_id, bearer_token):
        base_url = "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/{project_id}/locations/us-central1/endpoints/{endpoint_id}:predict"
        self.full_url = base_url.format(project_id=project_id, endpoint_id=endpoint_id)
        self.bearer_token = bearer_token

    def call_llama(self, prompt, max_length=200, top_k=10):
        request_body = {
            "instances": [
                {
                    "prompt": prompt,
                    "max_length": max_length,
                    "top_k": top_k
                }
            ]
        }
        headers = {
            "Authorization": "Bearer {bearer_token}".format(bearer_token=self.bearer_token),
            "Content-Type": "application/json"
        }
        resp = requests.post(self.full_url, json=request_body, headers=headers)
        return resp.json()

if __name__ == "__main__":
    SCOPES = ['https://www.googleapis.com/auth/cloud-platform']
    SERVICE_ACCOUNT_FILE = 'orchestra-handler-service-account.json'

    cred = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)

    auth_req = google.auth.transport.requests.Request()
    cred.refresh(auth_req)
    bearer_token = cred.token

    project_id = "saas-368716"
    endpoint_id = "9143839962771226624"

    llama = VertexLlama(project_id, endpoint_id, bearer_token)
    print(llama.call_llama("Write a poem about Guillermo.", max_length=100, top_k=10))