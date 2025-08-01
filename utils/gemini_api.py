import requests

# Gemini API configuration
GEMINI_API_KEY = "AIzaSyAufdd2Tsje03N9Ki3RHDpGKxB_RpOoCUc"
GEMINI_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
)

def ask_gemini(prompt: str) -> str:
    headers = {
        "Content-Type": "application/json"
    }
    
    data = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ]
    }

    response = requests.post(
        GEMINI_ENDPOINT,
        params={"key": GEMINI_API_KEY},
        headers=headers,
        json=data
    )

    if response.status_code == 200:
        try:
            return response.json()['candidates'][0]['content']['parts'][0]['text']
        except (KeyError, IndexError) as e:
            return f"Unexpected response format: {response.json()}"
    else:
        return f"Error {response.status_code}: {response.text}"
