from .moderation import Moderation
from .jail import Jail

async def setup(bot):
    await bot.add_cog(Moderation(bot))
    await bot.add_cog(Jail(bot))
