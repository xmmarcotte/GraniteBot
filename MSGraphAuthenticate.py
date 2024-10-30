import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from msal import ConfidentialClientApplication
from threading import Event
from urllib.parse import urlparse, parse_qs
import os
import dotenv
import requests
import json
from threading import Thread
import re

dotenv.load_dotenv()


class Authenticate:
    def __init__(self):
        self.tenant_id = os.getenv('MS_TENANT_ID')
        self.client_id = os.getenv('MS_CLIENT_ID')
        self.client_secret = os.getenv('MS_CLIENT_SECRET')
        self.redirect_uri = "http://localhost:8888"
        self.authorize_url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/authorize?client_id={self.client_id}&scope=https://graph.microsoft.com/.default offline_access&response_type=code&redirect_uri={self.redirect_uri}"
        self.token_file_path = "token.json"
        self.scope = ["https://graph.microsoft.com/.default"]

        self.app = ConfidentialClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            client_credential=self.client_secret,
        )

        self.session = requests.Session()
        self.auth_completed_event = Event()

    def start_http_server(self, port=8888):
        server_address = ("127.0.0.1", port)
        httpd = HTTPServer(server_address, lambda *args, **kwargs: RedirectHandler(self, *args, **kwargs))
        httpd.handle_request()

    def authenticate(self):
        token_response = self.load_token_from_file()

        if token_response and 'access_token' in token_response:
            if not self.is_token_expired(token_response) and self.test_token_validity(token_response):
                return token_response
            elif 'refresh_token' in token_response:
                silent_response = self.acquire_token_by_refresh_token(token_response['refresh_token'])
                if silent_response:
                    return silent_response

        # print("Attempting interactive token acquisition.")
        return self.acquire_new_token()

    def acquire_token_by_refresh_token(self, refresh_token):
        token_response = self.app.acquire_token_by_refresh_token(refresh_token, scopes=self.scope)

        if "access_token" in token_response:
            self.save_token_to_file(token_response)
            # print("Refresh token acquired!")
            return token_response
        else:
            print("Failed to acquire token by refresh token. Error: ", token_response.get("error_description"))
            return None

    def delete_token_file(self):
        if os.path.exists(self.token_file_path):
            os.remove(self.token_file_path)
            # print("Token file deleted.")

    def test_token_validity(self, token_response):
        # Make a test API call using the access token
        graph_url = "https://graph.microsoft.com/v1.0/users"
        headers = {
            "Authorization": f"Bearer {token_response['access_token']}",
            "Accept": "application/json"
        }
        params = {
            "$filter": f"userPrincipalName eq 'mmarcotte@granitenet.com'"
        }

        response = self.session.get(graph_url, headers=headers, params=params)
        if response.status_code == 200:
            return True

    def save_token_to_file(self, token_response):
        with open(self.token_file_path, 'w') as file:
            json.dump(token_response, file)
            # print("Token saved to file.")

    def load_token_from_file(self):
        if os.path.exists(self.token_file_path):
            with open(self.token_file_path, 'r') as file:
                return json.load(file)
        return None

    @staticmethod
    def is_token_expired(token_response):
        try:
            expiration_seconds = int(token_response['expires_in'])
        except KeyError:
            print("Error: 'expires_in' not found in token response.")
            return False

        current_timestamp = int(time.time())

        if expiration_seconds <= 0:
            return 'refresh_token' not in token_response

        # Calculate the absolute expiration time ('expires_on')
        expires_on = current_timestamp + expiration_seconds

        return current_timestamp >= expires_on

    def acquire_new_token(self):
        max_retries = 3  # Set a maximum number of retry attempts
        retry_delay = 5  # Set a delay between retries (in seconds)

        for _ in range(max_retries):
            # Start the HTTP server on a separate thread
            server_thread = Thread(target=self.start_http_server)
            server_thread.start()

            # Open the authorization URL in the default web browser
            webbrowser.open(self.authorize_url, new=2, autoraise=True)

            # Wait for the authentication process to complete or timeout
            auth_completed = self.auth_completed_event.wait(timeout=60)

            # Close the HTTP server
            server_thread.join()

            # Check if the authentication was completed and token response is available
            if auth_completed and self.token_response:
                # Save the token to the file
                self.save_token_to_file(self.token_response)
                return self.token_response
            else:
                print("Error: Authentication may not have been completed. Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)

        print("Error: Authentication failed after multiple retries.")
        return None


class RedirectHandler(BaseHTTPRequestHandler):
    def __init__(self, authenticate, *args, **kwargs):
        self.authenticate = authenticate
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path == '/favicon.ico':
            self.send_error(404)
            return

        query = urlparse(self.path).query
        params = parse_qs(query)
        auth_code = params.get("code", [""])[0]

        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><head><title>Authentication Complete</title></head>")
        self.wfile.write(b"<body><p>Authentication completed. This window will close shortly.</p></body></html>")

        if auth_code:
            try:
                token_response = self.authenticate.app.acquire_token_by_authorization_code(
                    auth_code,
                    scopes=self.authenticate.scope,
                    redirect_uri=self.authenticate.redirect_uri,
                )

                if "access_token" in token_response:
                    self.authenticate.token_response = token_response
                    self.authenticate.auth_completed_event.set()
                else:
                    raise ValueError("Access token not found in token response")

            except Exception as e:
                print(f"Error during token retrieval: {e}")

        self.wfile.write(b"<script>window.close();</script>")


class TeamsSearch:
    def __init__(self, authenticate):
        self.authenticate = authenticate
        self.graph_base_url = "https://graph.microsoft.com/beta"

    def get_headers(self):
        token = self.authenticate.authenticate()
        if token and 'access_token' in token:
            return {"Authorization": f"Bearer {token['access_token']}", "Content-Type": "application/json"}
        print("Authentication failed.")
        return None

    def search_teams_messages(self, search_term, size=100):
        headers = self.get_headers()
        search_payload = {
            "requests": [{
                "entityTypes": ["chatMessage"],
                "query": {"queryString": f"\"{search_term}\" OR \"CW{search_term}-1\" OR \"0{search_term}\""},
                "from": 0,
                "size": size
            }]
        }
        response = requests.post(f"{self.graph_base_url}/search/query", headers=headers, json=search_payload,
                                 timeout=60)
        return response.json().get('value', [])[0].get('hitsContainers', [])[0].get('hits', [])

    def get_conversations(self, search_term):
        top_threads = self.search_teams_messages(search_term)
        conversations = {}

        for thread in top_threads:
            resource = thread['resource']
            message_id = resource['id']
            channel_id = resource.get('channelIdentity', {}).get('channelId')
            team_id = self.get_actual_team_id_for_message(channel_id) or resource.get('channelIdentity', {}).get(
                'teamId')

            if team_id:
                # Get conversation messages
                conversation_messages = self.get_channel_message_thread(team_id, channel_id, message_id)
                # Check if there are non-empty messages before adding to conversations
                if conversation_messages:  # Only add if there's at least one message
                    conversations[message_id] = conversation_messages
                else:
                    # print(f"No valid messages found for message ID: {message_id}")
                    pass
            else:
                # print(f"Could not verify team ID for message ID: {message_id}")
                pass

        return conversations

    def get_actual_team_id_for_message(self, channel_id):
        headers = self.get_headers()
        response = requests.get(f"{self.graph_base_url}/me/joinedTeams", headers=headers)
        teams = response.json().get('value', [])

        for team in teams:
            team_id = team['id']
            response = requests.get(f"{self.graph_base_url}/teams/{team_id}/channels", headers=headers)
            channels = response.json().get('value', [])
            if any(channel['id'] == channel_id for channel in channels):
                return team_id
        # print("Could not find team for channel ID.")
        return None

    def get_channel_message_thread(self, team_id, channel_id, message_id):
        headers = self.get_headers()
        # Fetch the main message
        main_message = self.fetch_message(
            f"{self.graph_base_url}/teams/{team_id}/channels/{channel_id}/messages/{message_id}", headers)

        messages = []

        # Check for an error in the main message response
        if 'error' in main_message:
            # print(f"Error fetching message ID {message_id}: {main_message['error']['message']}")
            return messages  # Return an empty list for this message ID

        # Debug output to inspect the main message structure
        # print('Main message:', main_message)

        # Continue processing if no error is found
        sent_by = self.get_sender_name(main_message)
        content = self.handle_special_messages(main_message)

        messages.append({
            'timestamp': main_message.get('createdDateTime', 'No timestamp available'),
            'sent_by': sent_by,
            'content': content
        })

        # Fetch replies to the main message
        replies = self.fetch_replies(
            f"{self.graph_base_url}/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies", headers)

        for reply in replies:
            sent_by = self.get_sender_name(reply)
            content = self.handle_special_messages(reply)

            messages.append({
                'timestamp': reply.get('createdDateTime', 'No timestamp available'),
                'sent_by': sent_by,
                'content': content
            })

        return messages

    def handle_special_messages(self, message):
        """Handles messages that may contain special formats like adaptive cards."""
        content = message.get('body', {}).get('content', '')

        # Clean HTML content
        cleaned_content = self.clean_html(content)

        # Check if there are attachments
        if 'attachments' in message:
            for attachment in message['attachments']:
                if attachment['contentType'] == 'application/vnd.microsoft.card.adaptive':
                    try:
                        # Convert the escaped JSON content back to a JSON object
                        card_content = json.loads(attachment['content'])
                        extracted_text = self.extract_text_from_adaptive_card(card_content)
                        cleaned_content += "\n" + extracted_text
                    except json.JSONDecodeError as e:
                        print("Error decoding adaptive card content:", e)

        return cleaned_content.strip()  # Remove leading/trailing whitespace

    def extract_text_from_adaptive_card(self, card_data):
        """Recursively extracts text from the adaptive card JSON structure."""
        texts = []

        def extract_recursive(elements):
            for element in elements:
                if element.get('type') == 'TextBlock':
                    text = element.get('text', '')
                    text = self.clean_text(text)
                    texts.append(text)
                elif element.get('type') == 'FactSet':
                    for fact in element.get('facts', []):
                        title = fact.get('title', '')
                        value = fact.get('value', '')
                        texts.append(f"{title} {value}")
                elif element.get('type') == 'Container':
                    extract_recursive(element.get('items', []))
                elif element.get('type') == 'ColumnSet':
                    for column in element.get('columns', []):
                        extract_recursive(column.get('items', []))

        extract_recursive(card_data.get('body', []))
        return " | ".join(texts)  # Joining with pipes

    def clean_text(self, text):
        """Cleans up text by removing unwanted formatting."""
        text = re.sub(r'<at id="[^"]+">', '', text)  # Remove <at> opening tags
        text = re.sub(r'</at>', '', text)  # Remove <at> closing tags
        text = text.replace('&nbsp;', ' ')  # Replace non-breaking spaces
        text = text.replace('&quot;', '"')  # Replace escaped quotes
        text = text.replace('**', '')  # Remove markdown bold syntax
        return text

    def clean_html(self, content):
        """Removes HTML tags and unwanted characters from the content."""
        clean_content = re.sub(r'<[^>]+>', '', content)  # Remove HTML tags
        clean_content = re.sub(r'&nbsp;', ' ', clean_content)  # Replace non-breaking spaces
        return clean_content.strip()

    def get_sender_name(self, message):
        """Extracts the sender's name from the message."""
        if 'from' in message and message['from'].get('user'):
            return message['from']['user'].get('displayName', 'Unknown Sender')
        return 'Unknown Sender'

    def fetch_message(self, url, headers):
        return requests.get(url, headers=headers).json()

    def fetch_replies(self, url, headers):
        replies = []
        while url:
            response = requests.get(url, headers=headers).json()
            replies += response.get('value', [])
            url = response.get('@odata.nextLink')
        return replies


if __name__ == "__main__":
    authenticate = Authenticate()
    teams_search = TeamsSearch(authenticate)

    print(json.dumps(teams_search.get_conversations('3525407'), indent=2))
