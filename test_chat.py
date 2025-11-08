from flask import Flask, render_template_string, session

app = Flask(__name__)
app.secret_key = "test-secret"

TEMPLATE = """
<!doctype html>
<title>Test Chat</title>
<style>
#chatContainer { height: 200px; border: 1px solid #ccc; padding: 10px; }
</style>

<h3>Chat Test</h3>
<div id="chatContainer">
  <div id="chatMessages">
    {% set has_messages = chat_history and chat_history|length > 0 %}
    {% if has_messages %}
      <p>Has {{chat_history|length}} messages:</p>
      {% for message in chat_history %}
        <div>{{message.role}}: {{message.content}}</div>
      {% endfor %}
    {% else %}
      <p><i>No messages yet - start a conversation...</i></p>
    {% endif %}
  </div>
</div>

<p>Debug info:</p>
<ul>
  <li>chat_history: {{ chat_history }}</li>
  <li>chat_history type: {{ chat_history.__class__.__name__ }}</li>
  <li>chat_history length: {{ chat_history|length if chat_history else 'None' }}</li>
  <li>has_messages: {{ chat_history and chat_history|length > 0 }}</li>
</ul>
"""

@app.route("/")
def test():
    # Simulate what your main app does
    chat_history = session.get("chat_history", [])
    if not chat_history:
        chat_history = []
    
    return render_template_string(TEMPLATE, chat_history=chat_history)

@app.route("/add")
def add():
    if "chat_history" not in session:
        session["chat_history"] = []
    session["chat_history"].append({"role": "user", "content": "Test message"})
    return "Added message, <a href='/'>go back</a>"

@app.route("/clear")
def clear():
    session.pop("chat_history", None)
    return "Cleared, <a href='/'>go back</a>"

if __name__ == "__main__":
    app.run(debug=True, port=5066)