"""
Example 07: Function / Tool Calling with DeepSeek
"""

from deepseek import DeepSeekClient, tool


@tool
def get_weather(city: str, unit: str = "celsius") -> str:
    """Get the current weather in a city.

    Args:
        city: The city name (e.g. "Tokyo").
        unit: Temperature unit, "celsius" or "fahrenheit".
    """
    return f"22° {unit} in {city}, sunny"


def main():
    client = DeepSeekClient()

    print("--- 1. Auto Approval Mode ---")
    reply = client.chat_with_tools(
        "What is the weather in Tokyo?",
        tools=[get_weather],
        approval="auto",
    )
    print("Final Answer:", reply.text)
    print("Tool Call Requested:", reply.tool_call)
    print("Tool Calls Made:", reply.tool_calls_made)

    print("\n--- 2. Manual Approval Mode ---")
    # Custom approval callback simulating manual choice
    def manual_approval_cb(call):
        print(f"[Approval Callback] Model asked to run: {call.name}({call.arguments})")
        return True

    reply_manual = client.chat_with_tools(
        "What is the weather in Paris?",
        tools=[get_weather],
        approval=manual_approval_cb,
    )
    print("Final Answer:", reply_manual.text)

    client.close()


if __name__ == "__main__":
    main()
