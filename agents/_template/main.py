import asyncio

from dispatchkit import Agent


class MyAgent(Agent):
    pass


if __name__ == "__main__":
    asyncio.run(MyAgent().run())
