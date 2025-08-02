import os
import asyncio
import logging
import json
import wave
import base64 
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn


from config import settings

from google import genai
from google.genai import types

# Import our GCS utility function and the settings object
from gcs_utils import upload_to_gcs

# Config
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

MODEL = settings.GEMINI_MODEL
RECORDINGS_DIR = settings.RECORDINGS_DIR
os.makedirs(RECORDINGS_DIR, exist_ok=True)

# FastAPI Setup
app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

# Gemini Client
client = genai.Client(api_key=settings.GEMINI_API_KEY.get_secret_value())

@app.websocket("/ws-ai")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logging.info("WebSocket connection accepted from: %s", websocket.client)

    try:
        while True:
            try:
                # use receive() as it 
                # This prevents a crash if binary data is received unexpectedly.
                message = await websocket.receive()
                if "text" in message:
                    message_json = json.loads(message["text"])
                    if message_json.get("type") == "start_call":
                        logging.info("Received start_call signal. Initializing Gemini Live session.")
                        break
                    else:
                        logging.warning("Received unexpected JSON message while idle: %s", message_json)
                elif "bytes" in message:
                     logging.warning("Received unexpected binary data while idle. Ignoring.")
                
            except (WebSocketDisconnect, RuntimeError):
                logging.info(f"Client {websocket.client} disconnected during idle phase. Closing connection.")
                return
            except (json.JSONDecodeError, KeyError):
                logging.warning(f"Received invalid message format {websocket.client} while idle. Ignoring.")
                continue
            except Exception as e:
                logging.error(f"Error in idle loop for {websocket.client}: {e}",exc_info=True)
                # You might want to close the connection here too.
                return
            

        # new gemini session creates here
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Check if the GCS bucket is configured before proceeding
        if settings.GCS_BUCKET_NAME:
             # Create a unique folder for each call session inside the bucket
            blob_folder = f"calls/{datetime.now().strftime('%Y/%m/%d')}/{session_id}/"
        else:
            blob_folder = None

        # These paths point to the temporary disk on the Render server
        user_wav_path = os.path.join(RECORDINGS_DIR, f"{session_id}_user.wav")
        gemini_wav_path = os.path.join(RECORDINGS_DIR, f"{session_id}_gemini.wav")

        user_wav_writer = wave.open(user_wav_path, 'wb')
        user_wav_writer.setnchannels(1); user_wav_writer.setsampwidth(2); user_wav_writer.setframerate(16000)

        gemini_wav_writer = wave.open(gemini_wav_path, 'wb')
        gemini_wav_writer.setnchannels(1); gemini_wav_writer.setsampwidth(2); gemini_wav_writer.setframerate(24000)

        #  Configure the session for both audio and vision from the start 
        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(language_code=settings.LANGUAGE_CODE),
            output_audio_transcription={},
            system_instruction=settings.SYSTEM_PROMPT,
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    silence_duration_ms=1000,
                    prefix_padding_ms=100,
                )
            ),
        )

        try:
            async with client.aio.live.connect(model=MODEL, config=live_config) as session:
                logging.info(f"Gemini Live multimodal session started successfully with model {MODEL}.")
                await websocket.send_json({"type": "call_started"})

                # This task now correctly handles both audio and video frames
                async def handle_user_input_task():
                    try:
                        while True:
                            message = await websocket.receive()
                            # binary audio data
                            if "bytes" in message:
                                data = message["bytes"]
                                user_wav_writer.writeframes(data)
                                await session.send(input={"data": data, "mime_type": "audio/pcm;rate=16000"})

                            elif "text" in message:
                                control_msg = json.loads(message["text"])
                                msg_type = control_msg.get("type")

                                if msg_type == "video_frame":
                                    img_data = base64.b64decode(control_msg["payload"])
                                    logging.info("Sending a video frame to Gemini.")
                                    # sending live frames 
                                    await session.send(input={"data": img_data, "mime_type": "image/jpeg"})

                                elif msg_type == "audio_stream_end":
                                    logging.info("Received audio_stream_end signal. Closing user input task.")
                                    break
                    except WebSocketDisconnect:
                        logging.info("Client disconnected during call. Closing user input task.")

                        # it correctly handles audio and text responses
                async def receive_responses_task():
                    try:
                        while True:
                            full_gemini_transcript = ""
                            async for response in session.receive():
                                # Safely check if the 'user_utterance' attribute exists before accessing it.
                                if hasattr(response, 'user_utterance') and (ut := response.user_utterance) and (uat := ut.output_transcription) and uat.text:
                                    await websocket.send_json({"type": "user_transcript", "text": uat.text.strip()})

                                if response.data:
                                    gemini_wav_writer.writeframes(response.data)
                                    await websocket.send_bytes(response.data)
                                
                                if (sc := response.server_content) and (oat := sc.output_transcription) and oat.text:
                                    transcript_chunk = oat.text
                                    full_gemini_transcript += transcript_chunk
                                    await websocket.send_json({"type": "gemini_chunk", "text": transcript_chunk})
                            if full_gemini_transcript:
                                logging.info("GEMINI said (full): %s", full_gemini_transcript.strip())
                    except WebSocketDisconnect:
                        logging.info("Client disconnected during call. Closing receive_responses_task.")

                send_task = asyncio.create_task(handle_user_input_task())
                receive_task = asyncio.create_task(receive_responses_task())

                done, pending = await asyncio.wait([send_task, receive_task], return_when=asyncio.FIRST_COMPLETED)
                for task in pending: task.cancel()
                for task in done:
                    if task.exception(): raise task.exception()

        except Exception as e:
            logging.error("An error occurred during the Gemini session: %s", e, exc_info=True)
        finally:
            logging.info("Closing WAV files for session...")
            user_wav_writer.close()
            gemini_wav_writer.close()

            # UPLOAD AND CLEANUP LOGIC 
            if blob_folder: # Only proceed if GCS is configured
                user_blob_name = f"{blob_folder}user.wav"
                gemini_blob_name = f"{blob_folder}gemini.wav"
                
                # Upload user recording and clean up if successful
                if upload_to_gcs(user_wav_path, user_blob_name):
                    os.remove(user_wav_path)
                
                # Upload gemini recording and clean up if successful
                if upload_to_gcs(gemini_wav_path, gemini_blob_name):
                    os.remove(gemini_wav_path)

            logging.info("Gemini session ended.")
            try:
                await websocket.send_json({"type": "call_ended"})
                logging.info("Sent call_ended signal. Awaiting next 'start_call' signal.")
            except (WebSocketDisconnect, RuntimeError):
                logging.info("Could not send 'call_ended' because client already disconnected.")
                raise WebSocketDisconnect

    except WebSocketDisconnect:
        logging.info("WebSocket connection closed by client.")
    except Exception as e:
        logging.error("An unhandled error occurred in the websocket endpoint: %s", e, exc_info=True)
    finally:
        if websocket.client:
            logging.info("Closing WebSocket connection for: %s", websocket.client)

if __name__ == "__main__":
    logging.info(f"Starting server on {settings.SERVER_HOST}:{settings.SERVER_PORT}")
    uvicorn.run(
        "main:app", 
        host=settings.SERVER_HOST, 
        port=settings.SERVER_PORT, 
        reload=True
    )