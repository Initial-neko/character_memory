from datetime import datetime,timedelta
class WorldClock:
    def __init__(self,now:datetime): self.current_time=now
    def advance(self,*,hours=0,days=0): self.current_time+=timedelta(hours=hours,days=days); return self.current_time
    def next_day(self): return self.advance(days=1)
    def simulate_days(self,days:int,callback):
        out=[]
        for _ in range(days): out.append(callback(self.current_time)); self.next_day()
        return out
